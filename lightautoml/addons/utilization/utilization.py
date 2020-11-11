from copy import deepcopy
from typing import Optional, Any, Sequence, Type, Union

from log_calls import record_history

from ...automl.base import AutoML
from ...automl.blend import Blender, BestModelSelector
from ...automl.presets.base import AutoMLPreset
from ...dataset.base import LAMLDataset
from ...ml_algo.base import MLAlgo
from ...pipelines.ml.base import MLPipeline
from ...tasks import Task
from ...utils.timer import PipelineTimer


@record_history(enabled=False)
class MLAlgoForAutoMLWrapper(MLAlgo):
    """
    Wrapper - it exists to apply blender to list of automl's

    """

    @classmethod
    def from_automls(cls, automl: Union[AutoML, Sequence[AutoML]]):
        ml_algo = cls()
        ml_algo.models.append(automl)

        return ml_algo

    def fit_predict(self, *args, **kwargs) -> LAMLDataset:
        raise NotImplementedError

    def predict(self, *args, **kwargs) -> LAMLDataset:
        raise NotImplementedError


@record_history(enabled=False)
class MLPipeForAutoMLWrapper(MLPipeline):
    """
    Wrapper - it exists to apply blender to list of automl's

    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ml_algos = self._ml_algos

    @classmethod
    def from_automl(cls, automl: AutoML):
        ml_pipe = cls([MLAlgoForAutoMLWrapper.from_automls(automl)])

        return ml_pipe

    @classmethod
    def from_blended(cls, automls: Sequence[AutoML], blender: Blender):
        ml_pipe = cls([MLAlgoForAutoMLWrapper.from_automls(automls), ])
        ml_pipe.blender = blender

        return ml_pipe


@record_history(enabled=False)
class TimeUtilization:
    """
    Class that helps to utilize given time to automl
    In best casd - blend different configurations of single preset
    In worst case - averaging multiple automl's with different states
    """

    def __init__(self, automl_factory: Type[AutoMLPreset],
                 task: Task,
                 timeout: int = 3600,
                 memory_limit: int = 16,
                 cpu_limit: int = 4,
                 gpu_ids: Optional[str] = None,
                 timing_params: Optional[dict] = None,
                 configs_list: Optional[Sequence[str]] = None,
                 inner_blend: Optional[Blender] = None,
                 outer_blend: Optional[Blender] = None,
                 drop_last: bool = True,
                 max_runs_per_config: int = 5,
                 random_state_keys: Optional[dict] = None,
                 random_state: int = 42,
                 **kwargs
                 ):
        """

        Args:
            automl_factory:
            task:
            timeout:
            memory_limit:
            cpu_limit:
            gpu_ids:
            timing_params:
            configs_list:
            inner_blend:
            outer_blend:
            drop_last:
            max_runs_per_config:
            random_state_keys: params of config that used as random state with initial values.
                If None - search for random_state key in default config of preset
                If not found - assume, that seeds are not fixed and each run is random by default
                For ex. {'reader_params': {'random_state': 42}, 'gbm_params': {'default_params': {'seed': 42}}}
            random_state: initial random_state value that will be set in case of search in config
        """

        self.automl_factory = automl_factory
        self.task = task
        self.timeout = timeout
        self.memoty_limit = memory_limit
        self.cpu_limit = cpu_limit
        self.gpu_ids = gpu_ids

        self.timing_params = timing_params
        if timing_params is None:
            self.timing_params = {}

        self.configs_list = configs_list
        if configs_list is None:
            self.configs_list = [None]

        self.max_runs_per_config = max_runs_per_config

        self.random_state_keys = random_state_keys
        if random_state_keys is None:
            self.random_state_keys = self._search_for_states(automl_factory, random_state)

        self.inner_blend = inner_blend
        if inner_blend is None:
            self.inner_blend = BestModelSelector()

        self.outer_blend = outer_blend
        if outer_blend is None:
            self.outer_blend = BestModelSelector()
        self.drop_last = drop_last
        
        self.kwargs = kwargs

    def _search_for_key(self, config, key, value: int = 42) -> dict:

        d = {}

        if key in config:
            d[key] = value

        for k in config:
            if type(config[k]) is dict:
                s = self._search_for_key(config[k], key, value)
                if len(s) > 0:
                    d[k] = s
        return d

    def _search_for_states(self, automl_factory: Type[AutoMLPreset], random_state: int = 42) -> dict:

        config = automl_factory.get_config()
        random_states = self._search_for_key(config, 'random_state', random_state)

        return random_states

    def _get_upd_states(self, random_state_keys: dict, upd_value: int = 0) -> dict:

        d = {}

        for k in random_state_keys:
            if type(random_state_keys[k]) is dict:
                d[k] = self._get_upd_states(random_state_keys[k], upd_value)
            else:
                d[k] = random_state_keys[k] + upd_value

        return d

    def fit_predict(self, train_data: Any, roles: dict, train_features: Optional[Sequence[str]] = None,
                    valid_data: Optional[Any] = None, valid_features: Optional[Sequence[str]] = None) -> LAMLDataset:
        """
        Same as automl's fit predict

        Args:
            train_data:
            roles:
            train_features:
            valid_data:
            valid_features:

        Returns:

        """
        timer = PipelineTimer(self.timeout, **self.timing_params).start()
        history = []

        amls = [[] for _ in range(len(self.configs_list))]
        aml_preds = [[] for _ in range(len(self.configs_list))]
        n_ms = 0
        n_cfg = 0
        upd_state_val = 0
        flg_continute = True
        # train automls one by one while timer is ok
        while flg_continute:
            n_ms += 1

            for n_cfg, config in enumerate(self.configs_list):
                random_states = self._get_upd_states(self.random_state_keys, upd_state_val)
                upd_state_val += 1
                print('CUR SETUP FOR RANDOM STATE: {}'.format(random_states))
                cur_kwargs = self.kwargs.copy()
                for k in random_states.keys():
                    if k in self.kwargs:
                        print('FOUND {} in kwargs, need to combine'.format(k))
                        random_states[k] = {**cur_kwargs[k], **random_states[k]}
                        del cur_kwargs[k]
                        print('MERGED VARIANT FOR {} = {}'.format(k, random_states[k]))
                automl = self.automl_factory(self.task, timer.time_left, memory_limit=self.memoty_limit,
                                             cpu_limit=self.cpu_limit, gpu_ids=self.gpu_ids,
                                             timing_params=self.timing_params,
                                             config_path=config, **random_states, **cur_kwargs)

                val_pred = automl.fit_predict(train_data, roles, train_features,
                                              valid_data, valid_features)

                amls[n_cfg].append(MLPipeForAutoMLWrapper.from_automl(automl))
                aml_preds[n_cfg].append(val_pred)

                history.append(timer.time_spent - sum(history))
                if timer.time_left < (sum(history) / len(history)) or \
                        upd_state_val >= (self.max_runs_per_config * len(self.configs_list)):
                    flg_continute = False
                    break

        # usually last model will be not complete due to timeout.
        # Maybe it's better to remove it from inner blend, which is typically just mean of models
        if n_ms > 1 and self.drop_last:
            amls[n_cfg].pop()
            aml_preds[n_cfg].pop()

        # prune empty algos
        amls = [x for x in amls if len(x) > 0]
        aml_preds = [x for x in aml_preds if len(x) > 0]

        # blend - first is inner blend - we blend same config with different states
        inner_pipes = []
        inner_preds = []

        for preds, pipes in zip(aml_preds, amls):
            inner_blend = deepcopy(self.inner_blend)
            val_pred, inner_pipe = inner_blend.fit_predict(preds, pipes)
            inner_pipe = [x.ml_algos[0].models[0] for x in inner_pipe]

            inner_preds.append(val_pred)
            inner_pipes.append(MLPipeForAutoMLWrapper.from_blended(inner_pipe, inner_blend))

        # outer blend - blend of blends
        val_pred, self.outer_pipes = self.outer_blend.fit_predict(inner_preds, inner_pipes)

        return val_pred

    def predict(self, data: Any, features_names: Optional[Sequence[str]] = None) -> LAMLDataset:
        """
        Same as automl's predict

        Args:
            data:
            features_names:

        Returns:

        """

        outer_preds = []

        for amls_pipe in self.outer_pipes:

            inner_preds = []
            # TODO: Maybe refactor?
            for automl in amls_pipe.ml_algos[0].models[0]:
                inner_pred = automl.predict(data, features_names)
                inner_preds.append(inner_pred)

            outer_pred = amls_pipe.blender.predict(inner_preds)
            outer_preds.append(outer_pred)

        pred = self.outer_blend.predict(outer_preds)

        return pred
