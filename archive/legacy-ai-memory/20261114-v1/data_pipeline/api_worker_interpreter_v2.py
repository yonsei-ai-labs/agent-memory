"""Version 2 of the bounded API language: Python augmented-assignment semantics.

Version 1 and all frozen reproduction bundles remain unchanged. New API
experiments must explicitly opt into this module and record VERSION + its hash.
This is still an experiment language, not a general Python sandbox.
"""
from __future__ import annotations

import ast
import operator

from data_pipeline.api_worker_interpreter import Interpreter as _Version1
from data_pipeline.api_worker_interpreter import UnsupportedCode, parse

VERSION = "api-worker-interpreter-v2"


class Interpreter(_Version1):
    def augmented_location(self, target):
        """Resolve the left-hand location once, before evaluating the RHS."""
        self.tick()
        if isinstance(target, ast.Name):
            if target.id in self.protected:
                raise UnsupportedCode("Reassignment of protected binding")
            value = self.expression(target)
            return value, lambda result: self.env.__setitem__(target.id, result)
        if isinstance(target, ast.Subscript):
            container = self.expression(target.value)
            index = self.expression(target.slice)
            value = container[index]
            return value, lambda result: operator.setitem(container, index, result)
        if isinstance(target, ast.Attribute):
            obj = self.expression(target.value)
            name = target.attr
            np, pd, sp = (self.modules[k] for k in ("np", "pd", "sp"))
            permitted = (
                isinstance(obj, np.ndarray) and name in ("dtype", "shape")
                or isinstance(obj, pd.DataFrame) and name in ("index", "columns")
                or sp.issparse(obj) and name in ("data", "indices", "indptr")
            )
            if not permitted:
                raise UnsupportedCode("Unsupported attribute assignment")
            value = self.attribute(obj, name)

            def store(result):
                if name == "shape":
                    self.bounded_shape(result)
                if name == "dtype":
                    self.bounded_dtype(result)
                setattr(obj, name, result)

            return value, store
        raise UnsupportedCode("Unsupported augmented assignment target")

    def block(self, statements):
        for node in statements:
            if not isinstance(node, ast.AugAssign):
                super().block([node])
                continue
            self.tick()
            current, store = self.augmented_location(node.target)
            other = self.expression(node.value)
            if isinstance(current, (list, tuple, str)) and isinstance(node.op, ast.Mult):
                if not isinstance(other, int) or len(current) * other > 1024:
                    raise UnsupportedCode("Container expansion limit")
            operation = {
                ast.Add: operator.iadd,
                ast.Sub: operator.isub,
                ast.Mult: operator.imul,
                ast.Div: operator.itruediv,
                ast.BitOr: operator.ior,
            }[type(node.op)]
            store(operation(current, other))
