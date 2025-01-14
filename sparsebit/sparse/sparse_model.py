from atexit import register
import copy
import operator
import importlib
from functools import partial
from fnmatch import fnmatch
from yacs.config import CfgNode as CN
from collections import defaultdict

import torch
import torch.nn as nn
import torch.fx as fx
import torch.nn.functional as F
import onnx

from sparsebit.utils import update_config
from sparsebit.sparse.modules import *
from sparsebit.quantization.converters import simplify  # FIXME

__all__ = ["SparseModel"]


class SparseModel(nn.Module):
    def __init__(self, model: nn.Module, config):
        super().__init__()
        self.model = model
        self.config = config
        self.device = torch.device(config.DEVICE)
        self._run_simplifiers()
        self._convert2sparsemodule()
        self._build_sparser()

    def _convert2sparsemodule(self):
        """
        将网络中部分node转成对应的sparse_module
        """
        named_modules = dict(self.model.named_modules(remove_duplicate=False))
        traced = fx.symbolic_trace(self.model)
        traced.graph.print_tabular()
        snodes = []  # 用于避免重复遍历
        for n in traced.graph.nodes:
            if not isinstance(n, fx.Node) or n in snodes:
                continue
            elif n.op == "call_module":
                assert n.target in named_modules, "no found {} in model".format(
                    n.target
                )
                if type(named_modules[n.target]) in SMODULE_MAP:
                    org_module = named_modules[n.target]
                    new_module = SMODULE_MAP[type(org_module)](org_module)
                else:
                    new_module = named_modules[n.target]
            elif n.op in [
                "call_function",
                "call_method",
                "placeholder",
                "get_attr",
                "output",
            ]:
                continue

            with traced.graph.inserting_after(n):
                traced.add_module(n.name, new_module)
                new_node = traced.graph.call_module(n.name, n.args, n.kwargs)
                snodes.append(new_node)
                n.replace_all_uses_with(
                    new_node
                )  # n的输出全部接到new_node, n成为no user节点(即可删除)

                traced.graph.erase_node(n)
        traced.recompile()
        self.model = fx.GraphModule(traced, traced.graph)

    def _build_sparser(self):
        """
        递归对每个SparseModule建立sparser
        """

        # build config for every SparseModule
        for n, m in self.model.named_modules():
            if isinstance(m, SparseOpr):
                _config = self.config.clone()  # init
                m.build_sparser(_config)

    def calc_params(self):
        for node in self.model.graph.nodes:
            if node.op == "call_module":
                module = getattr(self.model, node.target, None)
                if isinstance(module, SparseOpr) and getattr(module, "sparser", None):
                    module.calc_mask()

    def _run_simplifiers(self):
        self.model = simplify(self.model)

    def prepare_calibration(self):
        pass

    def forward(self, *args):
        return self.model.forward(*args)

    def export_onnx(
        self,
        dummy_data,
        name,
        input_names=None,
        output_names=None,
        dynamic_axes=None,
        opset_version=13,
        verbose=False,
        extra_info=False,
    ):
        self.eval()

        torch.onnx.export(
            self.model.cpu(),
            dummy_data.cpu(),
            name,
            opset_version=opset_version,
            input_names=input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            verbose=verbose,
        )
