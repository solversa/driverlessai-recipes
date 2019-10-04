"""CatBoost gradient boosting by Yandex. Currently supports regression and binary classification."""
import copy

import datatable as dt
import numpy as np
import _pickle as pickle
from sklearn.preprocessing import LabelEncoder

from h2oaicore.models import CustomModel, MainModel
from h2oaicore.systemutils import config, arch_type, physical_cores_count, ngpus_vis
from h2oaicore.systemutils import make_experiment_logger, loggerinfo, loggerwarning
from h2oaicore.models import LightGBMModel
import inspect


# https://github.com/KwokHing/YandexCatBoost-Python-Demo
# https://catboost.ai/docs/concepts/python-usages-examples.html
class CatBoostModel(CustomModel):
    _regression = True
    _binary = True
    _multiclass = True
    _display_name = "CatBoost"
    _description = "Yandex CatBoost GBM"
    _can_use_multi_gpu = False  # Can enable, but consumes too much memory
    # WIP: leakage can't find _catboost module, unsure what special.  Probably shift would fail too if used catboost.
    _can_use_gpu = True
    _force_gpu = False  # force use of GPU regardless of what DAI says
    _can_handle_categorical = True
    _can_handle_non_numeric = True
    _fit_by_iteration = True
    _fit_iteration_name = 'n_estimators'
    _predict_by_iteration = True
    _predict_iteration_name = 'ntree_end'

    _show_logger_test = True  # set to True to see how to send information to experiment logger
    _show_task_test = False  # set to True to see how task is used to send message to GUI

    def __init__(self, context=None,
                 unfitted_pipeline_path=None,
                 transformed_features=None,
                 original_user_cols=None,
                 date_format_strings=None,
                 **kwargs):

        super().__init__(context=context, unfitted_pipeline_path=unfitted_pipeline_path,
                         transformed_features=transformed_features, original_user_cols=original_user_cols,
                         date_format_strings=date_format_strings, **kwargs)

        self.input_dict = dict(context=context, unfitted_pipeline_path=unfitted_pipeline_path,
                               transformed_features=transformed_features,
                               original_user_cols=original_user_cols,
                               date_format_strings=date_format_strings, **kwargs)

    @staticmethod
    def is_enabled():
        return not (arch_type == "ppc64le")

    @staticmethod
    def do_acceptance_test():
        return True

    _modules_needed_by_name = ['catboost']

    def set_default_params(self,
                           accuracy=None, time_tolerance=None, interpretability=None,
                           **kwargs):
        # https://catboost.ai/docs/concepts/python-reference_parameters-list.html
        n_jobs = max(1, physical_cores_count)
        max_iterations = min(kwargs['n_estimators'],
                             config.max_nestimators) if 'n_estimators' in kwargs else config.max_nestimators
        max_iterations = min(kwargs['iterations'], max_iterations) if 'iterations' in kwargs else max_iterations
        self.params = {'iterations': max_iterations,
                       'learning_rate': config.min_learning_rate,
                       'train_dir': config.data_directory,
                       'allow_writing_files': False,
                       'thread_count': self.params_base.get('n_jobs', n_jobs),  # -1 is not supported
                       'early_stopping_rounds': 20
                       }
        #  https://catboost.ai/docs/concepts/python-reference_catboostclassifier.html
        # optimize for final model as transcribed from best lightgbm model
        n_estimators = self.params_base.get('n_estimators', 100)
        early_stopping_rounds = min(500, max(1, int(n_estimators / 4)))
        task_type = 'CPU' if self.params_base.get('n_gpus', 0) == 0 else 'GPU'
        if self._force_gpu:
            task_type = 'GPU'
        self.params = {'train_dir': config.data_directory,
                       'allow_writing_files': False,
                       'thread_count': self.params_base.get('n_jobs', n_jobs),  # -1 is not supported

                       'bootstrap_type': 'Bayesian',
                       'n_estimators': n_estimators,
                       'learning_rate': 0.018,
                       'random_state': self.params_base.get('random_state', 1234),
                       # 'metric_period': early_stopping_rounds,
                       'early_stopping_rounds': early_stopping_rounds,
                       'task_type': task_type,
                       'max_depth': 8,
                       'silent': self.params_base.get('silent', True),
                       }

        if self._can_handle_categorical:
            max_cat_to_onehot_list = [1, 4, 10, 20, 40, config.max_int_as_cat_uniques]
            self.params['one_hot_max_size'] = MainModel.get_one(max_cat_to_onehot_list, get_best=True)
            uses_gpus, n_gpus = self.get_uses_gpus(self.params)
            if uses_gpus:
                self.params['one_hot_max_size'] = min(self.params['one_hot_max_size'], 255)

        lgbm_params_example = {'booster': 'lightgbm', 'model_class_name': 'LightGBMModel', 'n_gpus': 0, 'gpu_id': 0,
                               'n_jobs': 5, 'num_classes': 2, 'num_class': 1, 'score_f_name': 'AUC',
                               'random_state': 889688271,
                               'pred_gap': None, 'pred_periods': None, 'n_estimators': 6000,
                               'learning_rate': 0.018000000000000002, 'early_stopping_rounds': 100, 'reg_alpha': 0.0,
                               'reg_lambda': 0.5, 'gamma': 0, 'max_bin': 64, 'scale_pos_weight': 1, 'max_delta_step': 0,
                               'min_child_weight': 1, 'subsample': 1, 'colsample_bytree': 0.35, 'tree_method': 'hist',
                               'grow_policy': 'lossguide', 'num_leaves': 64, 'max_depth': 0, 'min_data_in_bin': 1,
                               'min_child_samples': 1, 'boosting_type': 'gbdt', 'objective': 'xentropy',
                               'eval_metric': 'auc',
                               'monotonicity_constraints': False, 'max_cat_to_onehot': 10000, 'silent': True,
                               'seed': 889688271,
                               'disable_gpus': True, 'lossguide': False, 'dummy': False, 'accuracy': 7,
                               'time_tolerance': 10,
                               'interpretability': 1, 'ensemble_level': 2, 'train_shape': (590540, 1182),
                               'valid_shape': None,
                               'model_origin': 'DefaultIndiv: do_te:True,interp:1,depth:6,num_as_cat:False',
                               'subsample_freq': 1, 'min_data_per_group': 10, 'max_cat_threshold': 50,
                               'cat_smooth': 1.0,
                               'cat_l2': 1.0}

    def mutate_params(self,
                      **kwargs):
        fake_lgbm_model = LightGBMModel(**self.input_dict)
        fake_lgbm_model.params = self.params
        fake_lgbm_model.mutate_params(**kwargs)
        self.params = fake_lgbm_model.lightgbm_params
        self.params['bagging_temperature'] = MainModel.get_one([0, 0.1, 0.5, 0.9, 1.0])

        # see what else can mutate, need to know things don't want to preserve
        uses_gpus, n_gpus = self.get_uses_gpus(self.params)
        if not uses_gpus:
            self.params['colsample_bylevel'] = MainModel.get_one([0.3, 0.5, 0.9, 1.0])

        if not (uses_gpus and self.num_classes > 2):
            self.params['boosting_type'] = MainModel.get_one(['Plain', 'Ordered'])

        if self._can_handle_categorical:
            max_cat_to_onehot_list = [1, 4, 10, 20, 40, config.max_int_as_cat_uniques]
            self.params['one_hot_max_size'] = MainModel.get_one(max_cat_to_onehot_list)
            if uses_gpus:
                self.params['one_hot_max_size'] = min(self.params['one_hot_max_size'], 255)

    def fit(self, X, y, sample_weight=None, eval_set=None, sample_weight_eval_set=None, **kwargs):
        if self._show_logger_test:
            # Example use of logger, with required import of:
            #  from h2oaicore.systemutils import make_experiment_logger, loggerinfo
            # Can use loggerwarning, loggererror, etc. for different levels
            logger = None
            if self.context and self.context.experiment_id:
                logger = make_experiment_logger(experiment_id=self.context.experiment_id, tmp_dir=self.context.tmp_dir,
                                                experiment_tmp_dir=self.context.experiment_tmp_dir)
            loggerinfo(logger, "TestLOGGER: Fit CatBoost")

        if self._show_task_test:
            # Example task sync operations
            if hasattr(self, 'testcount'):
                self.test_count += 1
            else:
                self.test_count = 0

            # The below generates a message in the GUI notifications panel
            if self.test_count == 0 and self.context and self.context.experiment_id:
                warning = "TestWarning: First CatBoost fit for this model instance"
                loggerwarning(logger, warning)
                task = kwargs.get('task')
                if task:
                    task.sync(key=self.context.experiment_id, progress=dict(type='warning', data=warning))
                    task.flush()

            # The below generates a message in the GUI top-middle panel above the progress wheel
            if self.test_count == 0 and self.context and self.context.experiment_id:
                message = "Tuning CatBoost"
                loggerinfo(logger, message)
                task = kwargs.get('task')
                if task:
                    task.sync(key=self.context.experiment_id, progress=dict(type='update', message=message))
                    task.flush()

        from catboost import CatBoostClassifier, CatBoostRegressor, EFstrType

        # label encode target and setup type of problem
        lb = LabelEncoder()
        if self.num_classes >= 2:
            lb.fit(self.labels)
            y = lb.transform(y)
            if eval_set is not None:
                valid_X = eval_set[0][0]
                valid_y = eval_set[0][1]
                valid_y = lb.transform(valid_y)
                eval_set = [(valid_X, valid_y)]
            self.params.update({'eval_metric': 'AUC', 'objective': 'Logloss'})
        if self.num_classes > 2:
            self.params.update({'eval_metric': 'AUC', 'objective': 'MultiClass'})

        if isinstance(X, dt.Frame):
            orig_cols = list(X.names)
            numeric_cols = list(X[:, [bool, int, float]].names)
        else:
            orig_cols = list(X.columns)
            numeric_cols = list(X.select_dtypes([np.number]).columns)

        # unlike lightgbm that needs label encoded categoricals, catboots can take raw strings etc.
        self.params['cat_features'] = [i for i, x in enumerate(orig_cols) if 'CatOrig:' in x or 'Cat:' in x or x not in numeric_cols]

        if not self.get_uses_gpus(self.params):
            # monotonicity constraints not available for GPU for catboost
            # get names of columns in same order
            X_names = list(dt.Frame(X).names)
            X_numeric = self.get_X_ordered_numerics(X)
            X_numeric_names = list(X_numeric.names)
            self.set_monotone_constraints(X=X_numeric, y=y, params=self.params)
            numeric_constraints = copy.deepcopy(self.params['monotone_constraints'])
            # if non-numerics, then fix those to have 0 constraint
            self.params['monotone_constraints'] = [0] * len(X_names)
            colnumi = 0
            for coli in X_names:
                if X_names[coli] in X_numeric_names:
                    self.params['monotone_constraints'][coli] = numeric_constraints[colnumi]
                    colnumi += 1

        if isinstance(X, dt.Frame) and len(self.params['cat_features']) == 0:
            # dt -> catboost internally using buffer leaks, so convert here
            # assume predict is after pipeline collection or in subprocess so needs no protection
            X = X.to_numpy()  # don't assign back to X so don't damage during predict
            X = np.ascontiguousarray(X, dtype=np.float32 if config.data_precision == "float32" else np.float64)
            if eval_set is not None:
                valid_X = eval_set[0][0].to_numpy()  # don't assign back to X so don't damage during predict
                valid_X = np.ascontiguousarray(valid_X,
                                               dtype=np.float32 if config.data_precision == "float32" else np.float64)
                valid_y = eval_set[0][1]
                eval_set = [(valid_X, valid_y)]

        X, eval_set = self.process_cats(X, eval_set, orig_cols)
        params = copy.deepcopy(self.params)  # keep separate, since then can be pulled form lightgbm params
        params = self.filter_params(params)

        if self._show_logger_test:
            loggerinfo(logger, "CatBoost parameters: %s %s" % (str(self.params), str(params)))

        if self.num_classes == 1:
            model = CatBoostRegressor(**params)
        else:
            model = CatBoostClassifier(**params)
        # Hit sometimes: Exception: catboost/libs/data_new/quantization.cpp:779: All features are either constant or ignored.
        if self.num_classes == 1:
            # assume not mae, which would use median
            # baseline = [np.mean(y)] * len(y)
            baseline = None
        else:
            baseline = None

        model.fit(X, y=y,
                  sample_weight=sample_weight,
                  baseline=baseline,
                  eval_set=eval_set,
                  early_stopping_rounds=kwargs.get('early_stopping_rounds', None)
                  )

        # https://catboost.ai/docs/concepts/python-reference_catboostclassifier.html
        # need to move to wrapper
        if model.get_best_iteration() is not None:
            iterations = model.get_best_iteration() + 1
        else:
            iterations = self.params['n_estimators'] + 1
        # must always set best_iterations
        self.set_model_properties(model=model,
                                  features=orig_cols,
                                  importances=model.feature_importances_,
                                  iterations=iterations)

    def process_cats(self, X, eval_set, orig_cols):
        # ensure catboost treats as cat by making str
        if len(self.params['cat_features']) > 0:
            X = X.to_pandas()
            if eval_set is not None:
                valid_X = eval_set[0][0]
                valid_y = eval_set[0][1]
                valid_X = valid_X.to_pandas()
                eval_set = [(valid_X, valid_y)]
            for coli in self.params['cat_features']:
                col = orig_cols[coli]
                if 'CatOrig:' in col:
                    cattype = str
                    # must be string for catboost
                elif 'Cat:' in col:
                    cattype = int
                else:
                    cattype = None
                if cattype is not None:
                    X[col] = X[col].astype(cattype)
                    if eval_set is not None:
                        valid_X = eval_set[0][0]
                        valid_y = eval_set[0][1]
                        valid_X[col] = valid_X[col].astype(cattype)
                        eval_set = [(valid_X, valid_y)]
        return X, eval_set

    def predict(self, X, **kwargs):
        model, features, importances, iterations = self.get_model_properties()
        # FIXME: Do equivalent throttling of predict size like def _predict_internal(self, X, **kwargs), wrap-up.
        if isinstance(X, dt.Frame) and len(self.params['cat_features']) == 0:
            # dt -> lightgbm internally using buffer leaks, so convert here
            # assume predict is after pipeline collection or in subprocess so needs no protection
            X = X.to_numpy()  # don't assign back to X so don't damage during predict
            X = np.ascontiguousarray(X, dtype=np.float32 if config.data_precision == "float32" else np.float64)

        X, eval_set = self.process_cats(X, None, self.feature_names_fitted)

        pred_contribs = kwargs.get('pred_contribs', None)
        output_margin = kwargs.get('output_margin', None)
        fast_approx = kwargs.pop('fast_approx', False)
        if fast_approx:
            kwargs['ntree_limit'] = min(config.fast_approx_num_trees, iterations - 1)
            kwargs['approx_contribs'] = pred_contribs
        else:
            kwargs['ntree_limit'] = iterations - 1

        # implicit import
        from catboost import CatBoostClassifier, CatBoostRegressor, EFstrType
        n_jobs = max(1, physical_cores_count)
        if not pred_contribs:
            if self.num_classes >= 2:
                preds = model.predict_proba(
                    data=X,
                    ntree_start=0,
                    ntree_end=iterations - 1,
                    thread_count=self.params_base.get('n_jobs', n_jobs),  # -1 is not supported
                )

                if preds.shape[1] == 2:
                    return preds[:, 1]
                else:
                    return preds
            else:
                return model.predict(
                    data=X,
                    ntree_start=0,
                    ntree_end=iterations - 1,
                    thread_count=self.params_base.get('n_jobs', n_jobs),  # -1 is not supported
                )
        else:
            # For Shapley, doesn't come from predict, instead:
            return model.get_feature_importance(
                data=X,
                ntree_start=0,
                ntree_end=iterations - 1,
                thread_count=self.params_base.get('n_jobs', n_jobs),  # -1 is not supported,
                type=EFstrType.ShapValues
            )
            # FIXME: Do equivalent of preds = self._predict_internal_fixup(preds, **mykwargs) or wrap-up

    def filter_params(self, params):
        from catboost import CatBoostClassifier, CatBoostRegressor, EFstrType
        fullspec_regression = inspect.getfullargspec(CatBoostRegressor)
        kwargs_regression = {k: v for k, v in zip(fullspec_regression.args, fullspec_regression.defaults)}
        fullspec_classification = inspect.getfullargspec(CatBoostClassifier)
        kwargs_classification = {k: v for k, v in zip(fullspec_classification.args, fullspec_classification.defaults)}

        if self.num_classes == 1:
            allowed_params = kwargs_regression
        else:
            allowed_params = kwargs_classification

        params_copy = copy.deepcopy(params)
        for k, v in params_copy.items():
            if k not in allowed_params.keys():
                del params[k]

        # now transcribe
        k = 'boosting_type'
        if k in params:
            params[k] = 'Plain'

        k = 'grow_policy'
        if k in params:
            params[k] = 'Depthwise' if params[k] == 'depthwise' else 'Lossguide'

        k = 'eval_metric'
        if k in params and params[k].upper() == 'AUC':
            params[k] = 'AUC'

        map = {'regression': 'RMSE', 'mae': 'MAE', "mape": 'MAPE', "huber": 'RMSE', "fair": 'RMSE', "rmse": "RMSE",
               "gamma": "RMSE", "tweedie": "RMSE", "poisson": "Poisson", "quantile": "Quantile", 'binary': 'Logloss',
               'auc': 'AUC', "xentropy": 'CrossEntropy'}
        k = 'objective'
        if k in params and params[k] in map.keys():
            params[k] = map[params[k]]

        k = 'eval_metric'
        if k in params and params[k] in map.keys():
            params[k] = map[params[k]]

        params.pop('verbose', None)
        params.pop('verbose_eval', None)
        params.pop('logging_level', None)

        if 'grow_policy' in params:
            if params['grow_policy'] == 'Lossguide':
                params.pop('max_depth', None)
            if params['grow_policy'] == 'Depthwise':
                params.pop('num_leaves', None)
        else:
            params['grow_policy'] = 'SymmetricTree'

        if 'task_type' not in params:
            params['task_type'] = 'CPU' if self.params_base.get('n_gpus', 0) == 0 else 'GPU'
            if self._force_gpu:
                params['task_type'] = 'GPU'

        uses_gpus, n_gpus = self.get_uses_gpus(params)

        if params['task_type'] == 'CPU':
            params.pop('grow_policy', None)
            params.pop('num_leaves', None)
            params.pop('max_leaves', None)
            params.pop('min_data_in_leaf', None)
            params.pop('min_child_samples', None)

        if params['task_type'] == 'GPU':
            params.pop('colsample_bylevel', None)  # : 0.35

        if 'grow_policy' in params and params['grow_policy'] in ['Depthwise', 'SymmetricTree']:
            if 'max_depth' in params and params['max_depth'] == -1:
                params['max_depth'] = max(2, int(np.log(params.get('num_leaves', 2 ** 6))))
        else:
            params.pop('max_depth', None)
            params.pop('depth', None)
        if 'grow_policy' in params and params['grow_policy'] == 'Lossguide':
            # if 'num_leaves' in params and params['num_leaves'] == -1:
            #    params['num_leaves'] = 2 ** params.get('max_depth', 6)
            if 'max_leaves' in params and params['max_leaves'] == -1:
                params['max_leaves'] = 2 ** params.get('max_depth', 6)
        else:
            params.pop('max_leaves', None)
            params.pop('num_leaves', None)

        if 'bootstrap_type' not in params or not params['bootstrap_type'] in ['Poisson', 'Bernoulli']:
            params.pop('subsample', None)

        params.update({'train_dir': config.data_directory,
                       'allow_writing_files': False,
                       'thread_count': self.params_base.get('n_jobs', 4)})

        if 'reg_lambda' in params and params['reg_lambda'] <= 0.0:
            params['reg_lambda'] = 3.0  # assume meant unset

        if 'max_cat_to_onehot' in params:
            params['one_hot_max_size'] = params['max_cat_to_onehot']
            if uses_gpus:
                params['one_hot_max_size'] = min(params['one_hot_max_size'], 255)
            params.pop('max_cat_to_onehot', None)

        params['max_bin'] = params.get('max_bin', 254)
        if params['task_type'] == 'CPU':
            params['max_bin'] = min(params['max_bin'], 254)  # https://github.com/catboost/catboost/issues/1010
        if params['task_type'] == 'GPU':
            params['max_bin'] = min(params['max_bin'], 127)  # https://github.com/catboost/catboost/issues/1010

        params['silent'] = False

        if uses_gpus:
            # https://catboost.ai/docs/features/training-on-gpu.html
            params['devices'] = "%d-%d" % (
            self.params_base.get('gpu_id', 0), self.params_base.get('gpu_id', 0) + n_gpus - 1)
            params['gpu_ram_part'] = 0.90  # per-GPU, assumes GPU locking or no other experiments running

        if self.num_classes > 2:
            params.pop("eval_metric", None)

        return params

    def get_uses_gpus(self, params):
        if 'task_type' not in params:
            params['task_type'] = 'CPU' if self.params_base.get('n_gpus', 0) == 0 else 'GPU'
            if self._force_gpu:
                params['task_type'] = 'GPU'

        n_gpus = self.params_base.get('n_gpus', 0)
        if self._force_gpu:
            n_gpus = 1
        if n_gpus == -1:
            n_gpus = ngpus_vis
        uses_gpus = params['task_type'] == 'GPU' and n_gpus > 0
        return uses_gpus, n_gpus
