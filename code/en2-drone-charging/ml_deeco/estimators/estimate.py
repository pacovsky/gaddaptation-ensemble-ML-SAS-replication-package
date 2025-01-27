"""
Estimates
"""
import abc
from collections import namedtuple, defaultdict
from enum import Enum, auto
from typing import Callable, List, TYPE_CHECKING, Optional, Dict

import numpy as np

from ml_deeco.estimators import Feature, TimeFeature
from ml_deeco.simulation import SIMULATION_GLOBALS

if TYPE_CHECKING:
    from ml_deeco.estimators import Estimator


BoundFeature = namedtuple('BoundFeature', ('name', 'feature', 'function'))


class DataCollectorMode(Enum):
    First = auto()  # keep only the first record for each recordId
    Last = auto()   # keep only the last record for each recordId
    All = auto()    # keep all records for each recordId


class DataCollector:

    def __init__(self, begin=DataCollectorMode.All):
        self._records = {}
        self._begin = begin
        self.x = []
        self.y = []

    def collectRecordInputs(self, recordId, x, extra=None, force_replace=False):
        if recordId not in self._records or force_replace:
            self._records[recordId] = []

        records = self._records[recordId]
        if self._begin == DataCollectorMode.All:
            records.append((x, extra))
        elif self._begin == DataCollectorMode.First:
            if len(records) == 0:
                records.append((x, extra))
        elif self._begin == DataCollectorMode.Last:
            if len(records) == 0:
                records.append((x, extra))
            else:
                records[0] = (x, extra)

    def collectRecordTargets(self, recordId, y):
        if recordId not in self._records:
            # the record with corresponding ID doesn't exist, the data probably weren't valid at the time
            return

        records = self._records[recordId]
        del self._records[recordId]

        for x, extra in records:
            self.x.append(x)
            if callable(y):
                self.y.append(y(x, extra))
            else:
                self.y.append(y)

    def clear(self):
        self.x = []
        self.y = []
        self._records = {}


class Estimate(abc.ABC):
    """
    Base class for ValueEstimate and TimeEstimate.
    """

    def __init__(self, **dataCollectorKwargs):
        # noinspection PyTypeChecker
        self.estimator: 'Estimator' = None
        self.inputs: List[BoundFeature] = []
        self.extras: List[BoundFeature] = []
        self.targets: List[BoundFeature] = []
        self.inputsIdFunction = lambda *args: (*args,)
        self.targetsIdFunction = lambda *args: (*args,)
        self.inputsGuards: List[Callable] = []
        self.targetsGuards: List[Callable] = []
        self.dataCollector = DataCollector(**dataCollectorKwargs)
        self.estimateCache: Dict[Dict] = dict()  # used only for estimates assigned to roles

    def using(self, estimator: 'Estimator'):
        """Assigns an estimator to the estimate."""
        self.estimator = estimator
        estimator.assignEstimate(self)
        return self

    @abc.abstractmethod
    def prepare(self):
        """The prepare function is called after all the decorators have initialized the inputs, targets, etc. and can be used to modify them."""
        pass

    def check(self):
        """Checks whether the estimate is initialized properly."""
        assert self.estimator is not None, "No estimator assigned, use the 'using' method to assign an estimator."
        assert self.inputsIdFunction is not None, f"{self.estimator.name}: 'inputsId' function not specified."
        assert self.targetsIdFunction is not None, f"{self.estimator.name}: 'targetsId' function not specified."
        assert len(self.inputs) > 0, f"{self.estimator.name}: No inputs specified."
        assert len(self.targets) > 0, f"{self.estimator.name}: No targets specified."

    # decorators (definition of inputs and outputs)

    def input(self, feature: Optional[Feature] = None):
        """Defines an input feature."""
        if feature is None:
            feature = Feature()

        def addInputFunction(function):
            self._addInput(function.__name__, feature, function)
            return function

        return addInputFunction

    def extra(self, function):
        """
        Defines an extra input feature – not given to the prediction model.

        We use this for example to save the time of the inputs in the time-to-condition estimate so that we can compute the time difference.
        """
        self._addExtra(function.__name__, function)
        return function

    def inputsId(self, function):
        """Defines the function for matching the inputs with according targets. Unless there is a specific need for modifying the default behavior, do not use this decorator."""
        self.inputsIdFunction = function
        return function

    def targetsId(self, function):
        """Defines the function for matching the targets with according inputs. Unless there is a specific need for modifying the default behavior, do not use this decorator."""
        self.targetsIdFunction = function
        return function

    def inputsValid(self, function):
        """Guard for detecting whether the inputs are valid and can be used for training. Use this as a decorator."""
        self.inputsGuards.append(function)
        return function

    def _addInput(self, name: str, feature: Feature, function: Callable):
        self.inputs.append(BoundFeature(name, feature, function))

    def _addExtra(self, name: str, function: Callable):
        self.extras.append(BoundFeature(name, None, function))

    def _addTarget(self, name: str, feature: Feature, function: Callable):
        self.targets.append(BoundFeature(name, feature, function))

    # estimation

    def _estimate(self, *args):
        """Helper function to compute the estimate."""
        x = self.generateRecord(*args)
        prediction = self.estimator.predict(x)
        return self.generateOutputs(prediction)

    def estimate(self, *args):
        """
        Computes the estimate (based on the current values of the attributes).

        Returns
        -------
        prediction : Any
            The predicted value (if there is only one target), or a dictionary `{ feature_name: predicted_value }` with all targets.
        """
        if self.estimateCache:  # the cache is non-empty
            ensemble, comp = args
            if comp in self.estimateCache[ensemble]:
                return self.estimateCache[ensemble][comp]
            else:
                return self._estimate(*args)

        return self._estimate(*args)

    def cacheEstimates(self, ensemble, components):
        """Computes the estimates for all components in a batch at the same time and caches the results. Use this only for estimates assigned to ensemble roles."""
        records = np.array([self.generateRecord(ensemble, comp) for comp in components])
        predictions = self.estimator.predictBatch(records)

        self.estimateCache[ensemble] = {
            comp: self.generateOutputs(prediction)
            for comp, prediction in zip(components, predictions)
        }

    def generateRecord(self, *args):
        """Generates the inputs record for the `Estimator.predict` function."""
        record = []
        for name, feature, function in self.inputs:
            value = function(*args)
            value = feature.preprocess(value)
            record.append(value)
        return np.concatenate(record)

    def generateOutputs(self, prediction):
        """Generates the outputs from the `Estimator.predict` prediction."""

        # if we have only one target, return just the value
        if len(self.targets) == 1:
            return self.targets[0][1].postprocess(prediction)

        # otherwise, return a dictionary with all the targets
        output = {}
        currentIndex = 0
        for name, feature, _ in self.targets:
            width = feature.getNumFeatures()
            values = prediction[currentIndex:currentIndex + width]
            output[name] = feature.postprocess(values)
            currentIndex += width

        return output

    def __get__(self, instance, owner):
        if instance is None:
            return self

        def estimate(*args):
            return self.estimate(instance, *args)

        return estimate

    # data collection

    def collectInputs(self, *args, x=None, id=None):
        """Collects the inputs for training."""
        for f in self.inputsGuards:
            if not f(*args):
                return

        if x is None:
            x = self.generateRecord(*args)

        extra = {
            name: function(*args)
            for name, _, function in self.extras
        }

        recordId = id if id is not None else self.inputsIdFunction(*args)
        self.dataCollector.collectRecordInputs(recordId, x, extra)

    def generateTargets(self, *args):
        """Generates the targets record for the training of `Estimator`."""
        record = []
        for name, feature, function in self.targets:
            value = function(*args)
            value = feature.preprocess(value)
            record.append(value)
        return np.concatenate(record)

    def collectTargets(self, *args, id=None):
        """Collects the targets for training."""
        for f in self.targetsGuards:
            if not f(*args):
                return

        y = self.generateTargets(*args)

        recordId = id if id is not None else self.targetsIdFunction(*args)
        self.dataCollector.collectRecordTargets(recordId, y)

    def getData(self, clear=True):
        """Gets (and optionally clears) all collected data."""
        x, y = self.dataCollector.x, self.dataCollector.y
        if clear:
            self.dataCollector.clear()
        return x, y


class ValueEstimate(Estimate):
    """
    Implementation of the value estimate (both regression and classification). Predicts a future value based on current observations.
    """

    def prepare(self):
        # nothing needed here
        pass

    def inTimeSteps(self, timeSteps):
        """Automatically collect the data with fixed time difference between inputs and targets."""
        self.targetsGuards.append(lambda *args: SIMULATION_GLOBALS.currentTimeStep >= timeSteps)
        self.inputsIdFunction = lambda *args: (*args, SIMULATION_GLOBALS.currentTimeStep)
        self.targetsIdFunction = lambda *args: (*args, SIMULATION_GLOBALS.currentTimeStep - timeSteps)
        return self

    def target(self, feature: Optional[Feature] = None):
        """Defines a target value."""
        if feature is None:
            feature = Feature()

        def addTargetFunction(function):
            self._addTarget(function.__name__, feature, function)
            return function

        return addTargetFunction

    def targetsValid(self, function):
        """Guard for detecting whether the targets are valid and can be used for training. Use this as a decorator."""
        self.targetsGuards.append(function)
        return function


class TimeEstimate(Estimate):
    """
    Implementation of the time-to-condition estimate.
    """

    def __init__(self, **dataCollectorKwargs):
        super().__init__(**dataCollectorKwargs)
        self.timeFunc = self.time(lambda *args: SIMULATION_GLOBALS.currentTimeStep)

        self.targets = [BoundFeature("time", TimeFeature(), None)]
        self.conditionFunctions = []

    def prepare(self):
        # The conditions work the same way as targets guards (false == invalid data), but we want to perform them after all the guards passed.
        self.targetsGuards += self.conditionFunctions

    def time(self, function):
        """Defines how to measure time for the time-to-condition estimate. The default uses the current time step of the simulation, so if the simulation is run using our `run_simulation`, there is no need to overriding the default behavior using this function."""
        self.timeFunc = function
        self.extras = [BoundFeature("time", TimeFeature(), self.timeFunc)]
        return function

    def condition(self, function):
        """Defines the condition for the time-to-condition estimate. If multiple conditions are defined, they are considered in an "and" manner."""
        self.conditionFunctions.append(function)
        return function

    def conditionsValid(self, function):
        """Guard for detecting whether the conditions are valid and can be used for training. Use this as a decorator."""
        self.targetsGuards.append(function)
        return function

    def generateTargets(self, *args):
        currentTimeStep = self.timeFunc(*args)

        def timeDifference(x, extra):
            difference = currentTimeStep - extra['time']
            return np.array([difference])

        return timeDifference


class ListWithEstimate(list):
    estimate: Optional[Estimate] = None
