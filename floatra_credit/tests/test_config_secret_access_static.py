# -*- coding: utf-8 -*-
"""Static guard: settings readers that return secrets must not be RPC-callable.

Odoo exposes every model method whose name does not start with ``_`` to any
logged-in user via ``/web/dataset/call_kw`` and XML-RPC, regardless of the
model's access rules. A public method that reads ``ir.config_parameter``
through ``sudo()`` therefore leaks the value to every internal or portal
user. This file parses ``models/res_config_settings.py`` (no Odoo runtime
needed — ``python3 tests/test_config_secret_access_static.py``) and fails if
any public method reads a Floatra config parameter.
"""

import ast
import os
import unittest

_MODEL = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "models", "res_config_settings.py",
    ),
)
_PARAM_PREFIX = "floatra_credit."


def _settings_class():
    with open(_MODEL, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "ResConfigSettings"
    )


def _reads_floatra_param(func):
    """True when the method calls ``get_param("floatra_credit.…")``."""
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get_param"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith(_PARAM_PREFIX)
        ):
            return True
    return False


class TestConfigSecretAccessStatic(unittest.TestCase):
    def setUp(self):
        self.methods = {
            n.name: n for n in _settings_class().body if isinstance(n, ast.FunctionDef)
        }

    def test_secret_readers_exist_and_are_private(self):
        for name in ("_get_floatra_config", "_get_webhook_secret", "_get_floatra_key_is_live"):
            self.assertIn(name, self.methods)
            self.assertTrue(_reads_floatra_param(self.methods[name]), name)

    def test_no_public_method_reads_a_floatra_param(self):
        leaking = [
            name for name, fn in self.methods.items()
            if not name.startswith("_") and _reads_floatra_param(fn)
        ]
        self.assertEqual(
            leaking, [],
            f"public (RPC-callable) methods read floatra_credit.* params: {leaking}",
        )


if __name__ == "__main__":
    unittest.main()
