"""Hierarchical domain organization.

Classes
-------
SearchSpace
    Hierarchical hyperparameter domain organization.
"""

import collections
import functools
import itertools
from multiprocessing.pool import ThreadPool
import operator
import warnings

import numpy as np
from sklearn.gaussian_process import GaussianProcessRegressor
from sklearn.gaussian_process.kernels import RBF

from pyrameter.domains.base import Domain
from pyrameter.methods.random import random_search
from pyrameter.trial import Trial


class SearchSpaceMeta(type):
    """Metaclass for handling behind-the-scenes tasks for SearchSpace objects.
    """

    def __new__(cls, name, bases, dct):
        x = super().__new__(cls, name, bases, dct)
        x._counter = itertools.count(0)
        return x


class SearchSpace(object, metaclass=SearchSpaceMeta):
    """Hierarchical hyperparameter domain organization and value generation.

    A search space represents the set of hyperparameters corresponding to a
    single algorithm
    """

    def __init__(self, domains, exp_key=''):
        self.id = next(self.__class__._counter)
        self.exp_key = exp_key
        self.trials = []

        self._complexity = None
        self._uncertainty = None

        self.domains = domains if domains is not None else []
        self.domains.sort()

        self.done = False

    def __call__(self, method=None, to_dict=False):
        """Generate a new trial for this search space.

        Parameters
        ----------
        to_dict : bool
            Convert the hyperparameter values to a nested dictionary on return.

        Returns
        -------
        trial : ``pyrameter.trial.Trial`` or dict
            Trial data, including hyperparameter values and metadata for a
            database. If ``to_dict`` is ``True``, instead return only the
            nested dictionary of hyperparameter values matching the structure
            of the original specification.
        """
        if method is None:
            method = random_search
        hyperparameters = method(self)
        trial = Trial(self, hyperparameters=hyperparameters)
        self.trials.append(trial)
        return trial.parameter_dict if to_dict else trial

    def __eq__(self, other):
        return len(self.domains) == len(other.domains) and \
            all(map(lambda x: x[0] == x[1], zip(self.domains, other.domains)))

    @property
    def complexity(self):
        """Estimate the relative combinatorial complexity of this search space.

        Knowing the size of a search space can impact decision-making about how
        a search proceeds. In the case of multiple search spaces in the same
        search, it may be useful to schedule larger search spaces to faster
        hardware, for example. Complexity measures the size of this search
        space, normalized to a scale of [1, inf)
        """
        if self._complexity is None:
            self._complexity = functools.reduce(
                operator.mul,
                map(lambda d: d.complexity, self.domains),
                1.0)
        return self._complexity

    @classmethod
    def from_json(cls, obj):
        """Recreate a SearchSpace from its JSON representation.

        Parameters
        ----------
        obj : dict
            JSON encoding of the SearchSpace

        Returns
        -------
        searchspace : `pyrameter.searchspace.SearchSpace`
        """
        domains = [Domain.from_json(d) for d in obj['domains']]
        searchspace = cls(domains, exp_key=obj['exp_key'])
        trials = []
        for t in obj['trials']:
            trial = Trial(searchspace,
                          hyperparameters=t['hyperparameters'],
                          results=t['results'],
                          objective=t['objective'],
                          errmsg=t['errmsg'])
            trial.dirty = False
            trials.append(trial)
        searchspace.trials = trials
        searchspace._complexity = obj['complexity']
        searchspace._uncertainty = obj['uncertainty']
        return searchspace

    def generate(self):
        """Generate hyperparameters for this search space.

        Returns
        -------
        hyperparameters : list
            List of hyperparameters in order of domain name.
        """
        return [d.generate() for d in self.domains]

    def optimum(self, mode='min'):
        """Get the trial with the optimal performance.

        Parameters
        ----------
        mode : {'min','max'}
            If ``'min'``, return the trial with the minimum objective. If
            ``'max'``, return the trial with the maximum objective. Default:
            ``'min'``.

        Returns
        -------
        optimal : Trial
            The trial with the optimal observed value of the objective
            function.
        """
        return sorted([t for t in self.trials if t.objective is not None],
                      key=lambda x: x.objective,
                      reverse=(mode == 'max'))[0]

    def to_array(self):
        """Convert the trials in this search space into a contiguous array.

        Returns
        -------
        search_space: array_like
            Array of trials of shape ``(n_trials, n_domains + 1)``. Each row
            contains the value generated by each domain for the trial in order
            of domain name, with the value of the objective as the final entry
            in the row. If no trials have been conducted, returns ``None``.
        """
        if len(self.trials) > 0:
            out = np.zeros((len(self.trials), len(self.domains) + 1),
                  dtype=np.float32)

            for i, result in enumerate(self.trials):
                vec = [self.domains[j].map_to_domain(result.hyperparameters[j])
                       for j in range(len(self.domains))]
                vec.append(result.objective)
                out[i] += np.asarray(vec)
        else:
            out = None
        return out

    def to_json(self, simplify=False):
        """Convert this search space to a JSON-compatible representation."""
        jsonified = {
            'exp_key': self.exp_key,
            'complexity': self._complexity,
            'uncertainty': self._uncertainty
        }

        if not simplify:
            with ThreadPool() as p:
                domains = p.map(lambda x: x.to_json(), self.domains)
                trials = p.map(lambda x: x.to_json(), self.trials)
        else:
            with ThreadPool() as p:
                domains = p.map(lambda x: x.id, self.domains)
                trials = p.map(lambda x: x.id, self.trials)

        jsonified.update({'domains': domains, 'trials': trials})
        return jsonified

    @property
    def uncertainty(self):
        """Estimate the uncertainty in the performance of the search space.

        When trying to understand how different hyperparameters impact the
        performance of a model, the covariance between hyperparameter sets with
        respect to the objective can provide rich information about the model
        and search. Uncertainty is a heuristic that attempts to quantify how
        well a model's performance is understood based on multiple
        hyperparameterizations.

        Returns
        -------
        uncertainty : float
            An estimation of uncertainty in the performance of the model
            represented by this search space over a number of trials.
        """
        if len(self.trials) > 10:
            uncertainty_array = self.to_array()
            features = uncertainty_array[:, :-1]
            labels = uncertainty_array[:, -1]

            split = int(np.floor(labels.shape * 0.8))

            scales = np.zeros(50)
            for i in range(50):
                indices = np.random.permutation(np.arange(labels.shape[0]))
                est = np.random.uniform(0.1, 2.0)
                gp = GaussianProcessRegressor(kernel=RBF(length_scale=est),
                                              alpha=1e-5)
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    gp.fit(features[indices[:split]],
                           labels[indices[:split]])
                scales[i] += np.power(gp.kernel.theta[0], -1.0)

            self._uncertainty = np.linalg.norm(scales.max() - scales.min())
        else:
            self._uncertainty = 1

        return self._uncertainty
