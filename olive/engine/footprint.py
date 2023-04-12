# -------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
# --------------------------------------------------------------------------

import logging
from collections import OrderedDict, defaultdict
from datetime import datetime
from pathlib import Path

from olive.common.config_utils import ConfigBase, config_json_dumps, config_json_loads

logger = logging.getLogger(__name__)


class FootprintNodeMetric(ConfigBase):
    """
    value: {"metric_name": metrics_value, ...}
    cmp_direction: will be auto suggested. The format will be like: {"metric_name": 1, ...},
        1: higher is better, -1: lower is better
    is_goals_met: if the goals set by users is met
    """

    value: dict = None
    cmp_direction: dict = None
    is_goals_met: bool = False


class FootprintNode(ConfigBase):
    # None for no parent which means current model is the input model
    parent_model_id: str = None
    model_id: str
    model_config: dict = None
    from_pass: str = None
    pass_run_config: dict = None
    is_pareto_frontier: bool = False
    # TODO add EP/accelerators for same_model_id metrics
    metrics: FootprintNodeMetric = FootprintNodeMetric()

    date_time: float = datetime.now().timestamp()

    def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class Footprint:
    """
    The engine footprint is a class that contains the footprint of the engine runtime.
    It is used to collect the runtime state of the Olive engine and to organize the state
    in a way that is easy to visualize and understand for customers.
    """

    def __init__(
        self,
        nodes: OrderedDict = None,
        objective_dict: dict = None,
        is_marked_pareto_frontier: bool = False,
    ):
        self.nodes = nodes if nodes is not None else {}
        self.objective_dict = objective_dict if objective_dict is not None else {}
        self.is_marked_pareto_frontier = is_marked_pareto_frontier

    def record_objective_dict(self, objective_dict):
        self.objective_dict = objective_dict

    def _is_empty_metric(self, metric: FootprintNodeMetric):
        return not metric or not metric.value

    def resolve_metrics(self):
        for k, v in self.nodes.items():
            if v.metrics is None:
                continue
            if self.nodes[k].metrics.cmp_direction is None:
                self.nodes[k].metrics.cmp_direction = {}
            if self.nodes[k].metrics.value is None:
                self.nodes[k].metrics.value = {}

            is_goals_met = []
            for metric_name in v.metrics.value:
                if metric_name in self.objective_dict:
                    cmp_direction = 1 if self.objective_dict[metric_name]["higher_is_better"] else -1
                    self.nodes[k].metrics.cmp_direction[metric_name] = cmp_direction
                    _goal = self.objective_dict[metric_name]["goal"]
                    if _goal is None:
                        is_goals_met.append(True)
                    else:
                        is_goals_met.append(v.metrics.value[metric_name] * cmp_direction >= _goal)
                else:
                    logger.warning(f"Metric {metric_name} is not in the objective dict")
            self.nodes[k].metrics.is_goals_met = all(is_goals_met)

    def record(self, foot_print_node: FootprintNode = None, **kwargs):
        _model_id = kwargs.get("model_id", None)
        if foot_print_node is not None:
            _model_id = foot_print_node.model_id
            self.nodes[_model_id] = foot_print_node
        elif _model_id in self.nodes:
            self.nodes[_model_id].update(**kwargs)
        else:
            self.nodes[_model_id] = FootprintNode(**kwargs)
        self.resolve_metrics()

    def get_candidates(self):
        return {
            k: v
            for k, v in self.nodes.items()
            if not self._is_empty_metric(v.metrics) and v.parent_model_id is not None
        }

    def mark_pareto_frontier(self):
        if self.is_marked_pareto_frontier:
            return
        for k, v in self.nodes.items():
            # if current point's metrics is less than any other point's metrics, it is not pareto frontier
            cmp_flag = True and not self._is_empty_metric(v.metrics)
            for _, _v in self.nodes.items():
                if not cmp_flag:
                    break
                if not self._is_empty_metric(_v.metrics):
                    _against_pareto_frontier_check = True
                    # if all the metrics of current point is less than any other point's metrics,
                    # it is not pareto frontier e.g. current point's metrics is [1, 2, 3],
                    # other point's metrics is [2, 3, 4], then current point is not pareto frontier
                    # but if current point's metrics is [3, 2, 3], other point's metrics is [2, 3, 4],
                    # then current point is pareto frontier
                    for metric_name in v.metrics.value:
                        other_point_metrics = _v.metrics.value[metric_name] * _v.metrics.cmp_direction[metric_name]
                        current_point_metrics = v.metrics.value[metric_name] * v.metrics.cmp_direction[metric_name]
                        _against_pareto_frontier_check &= current_point_metrics < other_point_metrics
                    cmp_flag &= not _against_pareto_frontier_check
            self.nodes[k].is_pareto_frontier = cmp_flag
        self.is_marked_pareto_frontier = True

    def get_pareto_frontier(self):
        self.mark_pareto_frontier()
        rls = {k: v for k, v in self.nodes.items() if v.is_pareto_frontier}
        for _, v in rls.items():
            logger.info(f"pareto frontier points: {v.model_id} {v.metrics.value}")

        # restructure the pareto frontier points to instance of Footprints node for further analysis
        return Footprint(nodes=rls, objective_dict=self.objective_dict, is_marked_pareto_frontier=True)

    def _get_metrics_name_by_indices(self, indices):
        rls = list()
        for _, v in self.nodes.items():
            if not self._is_empty_metric(v.metrics):
                for index in indices:
                    if isinstance(index, str):
                        assert index in v.metrics.value, f"the metric {index} is not in the metrics"
                        rls.append(index)
                    if isinstance(index, int):
                        assert index < len(v.metrics.value), f"the index {index} is out of range"
                        rls.append(list(v.metrics.value.keys())[index])
                return rls
        return rls

    def plot_pareto_frontier(self, index=None):
        if index is None:
            assert len(self.nodes) > 0, "you can not plot pareto frontier with empty nodes"
            index = [0, 1]
        self.mark_pareto_frontier()
        nodes_to_be_plotted = self.get_candidates()
        if not nodes_to_be_plotted:
            logger.warning("there is no candidate to be plotted.")
            return
        # plot pareto frontier
        try:
            import pandas as pd
            import plotly.graph_objects as go

            pd.options.mode.chained_assignment = None
        except Exception:
            logger.warning("Please make sure you installed pandas and plotly successfully.")
            return

        # select column to shown in pareto frontier chat
        metric_column = self._get_metrics_name_by_indices(index)
        # to support 3d pareto_frontier
        if len(metric_column) < 2:
            logger.error("you can not plot pareto frontier with less than 2 metrics")
            return
        dict_data = defaultdict(list)
        for k, v in nodes_to_be_plotted.items():
            dict_data["model_id"].append(k)
            dict_data["is_pareto_frontier"].append(v.is_pareto_frontier)
            dict_data["marker_color"].append("red" if v.is_pareto_frontier else "blue")
            dict_data["marker_size"].append(12 if v.is_pareto_frontier else 8)
            show_list = [k]
            for metric_name in metric_column:
                dict_data[metric_name].append(v.metrics.value[metric_name])
                show_list.append(f"{metric_name}: {v.metrics.value[metric_name]}")
            dict_data["show_text"].append("<br>".join(show_list))
        data = pd.DataFrame(dict_data)

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=dict_data[metric_column[index[0]]],
                y=dict_data[metric_column[index[1]]],
                mode="markers",
                name="all footprints",
                marker=dict(color=dict_data["marker_color"], size=dict_data["marker_size"]),
                customdata=dict_data["show_text"],
                hovertemplate="%{customdata}",
            )
        )
        pareto_frontiers_data = data.loc[data["is_pareto_frontier"]]
        pareto_frontiers_data.sort_values(metric_column, ascending=True, inplace=True)
        fig.add_trace(
            go.Scatter(
                x=pareto_frontiers_data[metric_column[index[0]]],
                y=pareto_frontiers_data[metric_column[index[1]]],
                mode="lines",
                name="pareto frontiers",
            )
        )
        size_params = {"width": 1000, "height": 600}
        fig.update_layout(xaxis_title=metric_column[index[0]], yaxis_title=metric_column[index[1]], **size_params)
        fig.show()

    def trace_back_run_history(self, model_id):
        """
        Trace back the run history of a model with the order of
        model_id -> parent_model_id1 -> parent_model_id2 -> ...
        """
        rls = OrderedDict()
        while model_id is not None:
            if model_id in rls:
                raise ValueError(f"Loop detected in the run history of model {model_id}")
            rls[model_id] = self.nodes[model_id].pass_run_config
            model_id = self.nodes[model_id].parent_model_id
        return rls

    def to_df(self):
        # to pandas.DataFrame
        pass

    def to_json(self):
        return config_json_dumps(self.nodes)

    @classmethod
    def from_json(cls, json_str):
        nodes = OrderedDict()
        for k, v in config_json_loads(json_str, object_pairs_hook=OrderedDict).items():
            nodes[k] = FootprintNode(**v)
        return cls(nodes=nodes)

    def to_file(self, file_path):
        with open(file_path, "w") as f:
            f.write(self.to_json())

    @classmethod
    def from_file(cls, file_path):
        with open(file_path, "r") as f:
            return cls.from_json(f.read())

    def get_model_inference_config(self, model_id):
        model_config = self.nodes[model_id].model_config
        if model_config is None:
            return None

        return model_config.get("config", {}).get("inference_settings", None)

    def get_model_path(self, model_id):
        model_config = self.nodes[model_id].model_config
        if model_config is None:
            return None

        return Path(model_config.get("config", {}).get("model_path", None))