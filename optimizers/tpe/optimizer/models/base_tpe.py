from abc import ABCMeta, abstractmethod
from typing import Callable, Dict, List, Optional, Tuple

import ConfigSpace as CS

import numpy as np

from parzen_estimator import (
    MultiVariateParzenEstimator,
    ParzenEstimatorType,
    build_categorical_parzen_estimator,
    build_numerical_parzen_estimator,
)

from optimizers.tpe.utils.constants import NumericType, config2type


class AbstractTPE(metaclass=ABCMeta):
    @abstractmethod
    def update_observations(
        self, eval_config: Dict[str, NumericType], results: Dict[str, float], runtime: float
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    def apply_knowledge_augmentation(self, observations: Dict[str, np.ndarray]) -> None:
        raise NotImplementedError

    @abstractmethod
    def compute_probability_improvement(self, config_cands: Dict[str, np.ndarray]) -> np.ndarray:
        raise NotImplementedError

    @abstractmethod
    def get_config_candidates(self) -> Dict[str, np.ndarray]:
        raise NotImplementedError

    @property
    @abstractmethod
    def observations(self) -> Dict[str, np.ndarray]:
        raise NotImplementedError


class BaseTPE(AbstractTPE, metaclass=ABCMeta):
    def __init__(
        self,
        config_space: CS.ConfigurationSpace,
        n_ei_candidates: int,
        objective_names: List[str],
        runtime_name: str,
        seed: Optional[int],
        min_bandwidth_factor: float,
        top: float,
        minimize: Optional[Dict[str, bool]],
    ):
        """
        Attributes:
            rng (np.random.RandomState): random state to maintain the reproducibility
            n_ei_candidates (int): The number of samplings to optimize the EI value
            config_space (CS.ConfigurationSpace): The searching space of the task
            hp_names (List[str]): The list of hyperparameter names
            objective_names (List[str]): The names of the metrics (or objective functions)
            runtime_name (str): The name of the runtime metric.
            observations (Dict[str, Any]): The storage of the observations
            sorted_observations (Dict[str, Any]): The storage of the observations sorted based on loss
            min_bandwidth_factor (float): The minimum bandwidth for numerical parameters
            top (float): The hyperparam of the cateogircal kernel. It defines the prob of the top category.
            is_categoricals (Dict[str, bool]): Whether the given hyperparameter is categorical
            is_ordinals (Dict[str, bool]): Whether the given hyperparameter is ordinal
        """
        self._rng = np.random.RandomState(seed)
        self._n_ei_candidates = n_ei_candidates
        self._config_space = config_space
        self._hp_names = list(config_space._hyperparameters.keys())
        self._objective_names = objective_names[:]
        self._runtime_name = runtime_name
        self._n_lower = 0
        self._percentile = 0.0
        self._min_bandwidth_factor = min_bandwidth_factor
        self._top = top
        self._minimize = {
            obj_name: True if minimize is None else minimize.get(obj_name, True) for obj_name in self._objective_names
        }

        self._observations: Dict[str, np.ndarray] = {hp_name: np.array([]) for hp_name in self._hp_names}
        self._sorted_observations: Dict[str, np.ndarray] = {hp_name: np.array([]) for hp_name in self._hp_names}
        self._observations.update({objective_name: np.array([]) for objective_name in objective_names})
        self._sorted_observations.update({objective_name: np.array([]) for objective_name in objective_names})
        self._observations[self._runtime_name] = np.array([])
        self._sorted_observations[self._runtime_name] = np.array([])

        self._is_categoricals = {
            hp_name: self._config_space.get_hyperparameter(hp_name).__class__.__name__ == "CategoricalHyperparameter"
            for hp_name in self._hp_names
        }

        self._is_ordinals = {
            hp_name: self._config_space.get_hyperparameter(hp_name).__class__.__name__ == "OrdinalHyperparameter"
            for hp_name in self._hp_names
        }
        self._mvpe_lower: MultiVariateParzenEstimator
        self._mvpe_upper: MultiVariateParzenEstimator
        self._order: np.ndarray

    @abstractmethod
    def _percentile_func(self) -> int:
        raise NotImplementedError

    @abstractmethod
    def _calculate_order(self, results: Optional[Dict[str, float]] = None) -> np.ndarray:
        raise NotImplementedError

    def apply_knowledge_augmentation(
        self, observations: Dict[str, np.ndarray], percentile_func: Optional[Callable] = None
    ) -> None:
        if any(self._observations[objective_name].size != 0 for objective_name in self._objective_names):
            raise ValueError("Knowledge augmentation must be applied before the optimization.")
        if any(objective_name not in observations for objective_name in self._objective_names):
            raise ValueError("All objectives must be provided for when applying knowledge augmentation")

        self._observations.update({name: vals.copy() for name, vals in observations.items()})
        order = self._calculate_order()
        self._sorted_observations.update({name: observations[name][order] for name in observations.keys()})
        self._n_lower = self._percentile_func() if percentile_func is None else percentile_func()
        n_observations = self._observations[self._objective_names[0]].size
        self._percentile = self._n_lower / n_observations
        self._update_parzen_estimators()

    def update_observations(
        self,
        eval_config: Dict[str, NumericType],
        results: Dict[str, float],
        runtime: float,
        percentile_func: Optional[Callable] = None,
    ) -> None:
        """
        Update the observations for the TPE construction

        Args:
            eval_config (Dict[str, NumericType]): The configuration to evaluate (after conversion)
            results (Dict[str, float]): The dict of loss values.
            runtime (float): The runtime for both sampling and training
        """
        order = self._calculate_order(results)

        for objective_name in self._objective_names:
            metric_val = results[0][objective_name]
            self._observations[objective_name] = np.append(self._observations[objective_name], metric_val)
            self._sorted_observations[objective_name] = self._observations[objective_name][order]

        for hp_name in self._hp_names:
            hp_val = eval_config[hp_name]
            self._observations[hp_name] = np.append(self._observations[hp_name], hp_val)
            self._sorted_observations[hp_name] = self._observations[hp_name][order]
        else:
            self._n_lower = self._percentile_func() if percentile_func is None else percentile_func()
            self._percentile = self._n_lower / self._observations[self._objective_names[0]].size
            self._update_parzen_estimators()
            self._observations[self._runtime_name] = np.append(self._observations[self._runtime_name], runtime)

    def _update_parzen_estimators(self) -> None:
        n_lower = self._n_lower
        pe_lower_dict: Dict[str, ParzenEstimatorType] = {}
        pe_upper_dict: Dict[str, ParzenEstimatorType] = {}
        for hp_name in self._hp_names:
            is_categorical = self._is_categoricals[hp_name]
            sorted_observations = self._sorted_observations[hp_name]

            # split observations
            lower_vals = sorted_observations[:n_lower]
            upper_vals = sorted_observations[n_lower:]

            pe_lower_dict[hp_name], pe_upper_dict[hp_name] = self._get_parzen_estimator(
                lower_vals=lower_vals,
                upper_vals=upper_vals,
                hp_name=hp_name,
                is_categorical=is_categorical,
            )

        self._mvpe_lower = MultiVariateParzenEstimator(pe_lower_dict)
        self._mvpe_upper = MultiVariateParzenEstimator(pe_upper_dict)

    def get_config_candidates(self) -> Dict[str, np.ndarray]:
        """
        Since we compute the probability improvement of each objective independently,
        we need to sample the configurations in advance.

        Returns:
            config_cands (Dict[str, np.ndarray]):
                A dict of arrays of candidates in each dimension
        """
        return self._mvpe_lower.sample(
            n_samples=self._n_ei_candidates, rng=self._rng, dim_independent=True, return_dict=True
        )

    def compute_config_loglikelihoods(self, config_cands: Dict[str, np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
        """
        Compute the probability improvement given configurations

        Args:
            config_cands (Dict[str, np.ndarray]):
                The dict of candidate values for each dimension.
                The length is the number of dimensions and
                each array has the length of n_ei_candidates.

        Returns:
            config_ll_lower, config_ll_upper (Tuple[np.ndarray]):
                The loglikelihoods of each configuration in
                the good group or bad group.
                The shape is (n_ei_candidates, ) for each.
        """
        config_ll_lower = self._mvpe_lower.log_pdf(config_cands)
        config_ll_upper = self._mvpe_upper.log_pdf(config_cands)
        return config_ll_lower, config_ll_upper

    def compute_probability_improvement(self, config_cands: Dict[str, np.ndarray]) -> np.ndarray:
        """
        Compute the (log) probability improvement given configurations

        Args:
            config_cands (Dict[str, np.ndarray]):
                The dict of candidate values for each dimension.
                The length is the number of dimensions and
                each array has the length of n_ei_candidates.

        Returns:
            config_ll_ratio (np.ndarray):
                The log of the likelihood ratios of each configuration.
                The shape is (n_ei_candidates, )

        Note:
            In this implementation, we consider the gamma
                (gamma + (1 - gamma)g(x)/l(x))^-1
                = exp(log(gamma)) + exp(log(1 - gamma) + log(g(x)/l(x)))
        """
        EPS = 1e-12
        cll_lower, cll_upper = self.compute_config_loglikelihoods(config_cands)
        first_term = np.log(self._percentile + EPS)
        second_term = np.log(1.0 - self._percentile + EPS) + cll_upper - cll_lower
        pi = -np.logaddexp(first_term, second_term)
        return pi

    def _get_parzen_estimator(
        self,
        lower_vals: np.ndarray,
        upper_vals: np.ndarray,
        hp_name: str,
        is_categorical: bool,
    ) -> Tuple[ParzenEstimatorType, ParzenEstimatorType]:
        """
        Construct parzen estimators for the lower and the upper groups and return them

        Args:
            lower_vals (np.ndarray): The array of the values in the lower group
            upper_vals (np.ndarray): The array of the values in the upper group
            hp_name (str): The name of the hyperparameter
            is_categorical (bool): Whether the given hyperparameter is categorical

        Returns:
            pe_lower (ParzenEstimatorType): The parzen estimator for the lower group
            pe_upper (ParzenEstimatorType): The parzen estimator for the upper group
        """
        pe_lower: ParzenEstimatorType
        pe_upper: ParzenEstimatorType

        config = self._config_space.get_hyperparameter(hp_name)
        config_type = config.__class__.__name__
        is_ordinal = self._is_ordinals[hp_name]
        kwargs = dict(config=config)

        if is_categorical:
            pe_lower = build_categorical_parzen_estimator(vals=lower_vals, **kwargs, top=self._top)
            pe_upper = build_categorical_parzen_estimator(vals=upper_vals, **kwargs, top=self._top)
        else:
            kwargs.update(
                dtype=config2type[config_type],
                is_ordinal=is_ordinal,
                default_min_bandwidth_factor=self._min_bandwidth_factor,
            )
            pe_lower = build_numerical_parzen_estimator(vals=lower_vals, **kwargs)
            pe_upper = build_numerical_parzen_estimator(vals=upper_vals, **kwargs)

        return pe_lower, pe_upper

    @property
    def size(self) -> int:
        n_evals = self._observations[self._objective_names[0]].size
        return n_evals

    @property
    def observations(self) -> Dict[str, np.ndarray]:
        n_evals = self.size
        return {hp_name: vals[-n_evals:].copy() for hp_name, vals in self._observations.items()}
