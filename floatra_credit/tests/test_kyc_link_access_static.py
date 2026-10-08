# -*- coding: utf-8 -*-
"""Static guard for the KYC-link access hardening (no Odoo runtime needed).

The behavioural tests live in ``test_floatra_credit.py`` and need
``odoo-bin --test-enable``. This file parses ``models/res_partner.py`` so a
plain ``python3 tests/test_kyc_link_access_static.py`` (or pytest) still
fails if someone drops the field ``groups=`` or the action guard:

* ``floatra_kyc_url`` / ``floatra_kyc_url_expires_at`` /
  ``floatra_kyc_url_active`` are restricted to the Floatra user group;
* both KYC-link actions call ``_floatra_check_kyc_link_access()`` as their
  first statement, and that helper raises AccessError unless the user has
  the group.
"""

import ast
import os
import unittest

_MODEL = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "models", "res_partner.py"),
)
GROUP = "floatra_credit.group_floatra_user"


def _tree():
    with open(_MODEL, encoding="utf-8") as f:
        return ast.parse(f.read())


def _class(tree, name="ResPartner"):
    return next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)


def _module_constant(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not defined")


class TestKycLinkAccessStatic(unittest.TestCase):
    def setUp(self):
        self.tree = _tree()
        self.cls = _class(self.tree)
        self.group_const = _module_constant(self.tree, "FLOATRA_USER_GROUP")

    def test_group_constant_is_the_floatra_user_group(self):
        self.assertEqual(self.group_const, GROUP)

    def test_kyc_link_fields_are_group_restricted(self):
        fields = {}
        for node in self.cls.body:
            if (
                isinstance(node, ast.Assign)
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Call)
            ):
                fields[node.targets[0].id] = node.value
        for name in (
            "floatra_kyc_url",
            "floatra_kyc_url_expires_at",
            "floatra_kyc_url_active",
        ):
            kwargs = {k.arg: k.value for k in fields[name].keywords}
            self.assertIn("groups", kwargs, name)
            self.assertIsInstance(kwargs["groups"], ast.Name, name)
            self.assertEqual(kwargs["groups"].id, "FLOATRA_USER_GROUP", name)

    def _method(self, name):
        return next(
            n for n in self.cls.body if isinstance(n, ast.FunctionDef) and n.name == name
        )

    def test_both_actions_check_access_first(self):
        for name in ("action_floatra_get_kyc_link", "action_floatra_email_kyc_link"):
            body = self._method(name).body
            # body[0] is the docstring.
            first = body[1]
            self.assertIsInstance(first, ast.Expr, name)
            call = first.value
            self.assertIsInstance(call, ast.Call, name)
            self.assertEqual(call.func.attr, "_floatra_check_kyc_link_access", name)

    def test_guard_raises_access_error_without_the_group(self):
        kyc = ast.unparse(self._method("_floatra_check_kyc_link_access"))
        self.assertIn("self._floatra_check_user_access(", kyc)
        src = ast.unparse(self._method("_floatra_check_user_access"))
        self.assertIn("has_group(FLOATRA_USER_GROUP)", src)
        self.assertIn("raise AccessError", src)


def _guard_line(func, guard):
    calls = [
        n.lineno for n in ast.walk(func)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == guard
    ]
    return min(calls) if calls else None


def _first_line_using(func, names):
    lines = [
        n.lineno for n in ast.walk(func)
        if (isinstance(n, ast.Name) and n.id in names)
        or (isinstance(n, ast.Attribute) and n.attr in names)
    ]
    return min(lines) if lines else None


class TestFloatraActionsAccessStatic(unittest.TestCase):
    """Onboarding and requesting credit write with sudo() and spend API
    calls: both check the Floatra user group before anything else."""

    SIDE_EFFECTS = {"FloatraAPIClient", "write", "sudo", "_floatra_refresh_lock_status"}

    def _check(self, path, cls_name, method, guard):
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        cls = _class(tree, cls_name)
        func = next(
            n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == method
        )
        guard_at = _guard_line(func, guard)
        self.assertIsNotNone(guard_at, method)
        self.assertLess(guard_at, _first_line_using(func, self.SIDE_EFFECTS), method)
        return tree, cls

    def test_onboard_checks_the_group_first(self):
        self._check(_MODEL, "ResPartner", "action_floatra_onboard",
                    "_floatra_check_user_access")

    def test_request_credit_checks_the_group_first(self):
        so = os.path.join(os.path.dirname(_MODEL), "sale_order.py")
        _tree_, cls = self._check(so, "SaleOrder", "action_request_floatra_credit",
                                  "_floatra_check_user_group")
        guard = next(
            n for n in cls.body
            if isinstance(n, ast.FunctionDef) and n.name == "_floatra_check_user_group"
        )
        src = ast.unparse(guard)
        self.assertIn("has_group(FLOATRA_USER_GROUP)", src)
        self.assertIn("raise AccessError", src)


if __name__ == "__main__":
    unittest.main()
