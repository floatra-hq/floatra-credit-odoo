# -*- coding: utf-8 -*-
"""Shared plumbing for the plain-Python contract tests.

The addon package ``__init__`` imports odoo, so the pure ``services/``
modules are loaded by path under a stand-in ``floatra_credit.services``
package. ``EXAMPLES`` are the inputs behind this connector's request
fixtures (``fixtures/contract/request-*.json``), which Floatra core validates
against its DTOs; ``write_request_fixtures()`` regenerates them.
"""

import importlib.util
import json
import os
import sys
import types
from datetime import datetime
from types import SimpleNamespace as NS

HERE = os.path.dirname(os.path.abspath(__file__))
SERVICES = os.path.normpath(os.path.join(HERE, "..", "services"))
FIXTURES = os.path.join(HERE, "fixtures", "contract")

_MODULES = (
    "floatra_contract",
    "pii_redaction",
    "kyc_link",
    "floatra_client",
    "floatra_api",
    "credit_lock",
)


def _ensure_pkg(name, path=None):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        mod.__path__ = [path] if path else []
        sys.modules[name] = mod


def load(modname):
    _ensure_pkg("floatra_credit")
    _ensure_pkg("floatra_credit.services", SERVICES)
    fq = "floatra_credit.services." + modname
    if fq not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            fq, os.path.join(SERVICES, modname + ".py"),
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[fq] = mod
        spec.loader.exec_module(mod)
    return sys.modules[fq]


services = NS(**{name: load(name) for name in _MODULES})


def fixture(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def fixture_names(prefix):
    return sorted(
        f[: -len(".json")]
        for f in os.listdir(FIXTURES)
        if f.startswith(prefix) and f.endswith(".json")
    )


# ----- Example records (Odoo-shaped stand-ins) -----

PARTNER = NS(
    id=77,
    name="Mama Nkechi Stores",
    mobile="0803 123 4567",
    phone=False,
    email="nkechi@example.com",
    street="12 Market Road",
    city="Ikeja",
    state_id=NS(name="Lagos"),
    floatra_business_type="RETAILER",
    floatra_primary_category="FMCG",
    floatra_assigned_agent_id="agt_01",
    floatra_owner_first_name="Nkechi",
    floatra_owner_last_name="Okafor",
)


def _line(category, subtotal):
    return NS(
        product_id=NS(categ_id=NS(floatra_category=category)),
        price_subtotal=subtotal,
    )


ORDER = NS(
    name="S00042",
    partner_id=PARTNER,
    amount_total=250000.0,
    floatra_tenure_days="14",
    order_line=[
        _line("FMCG", 200000.0),
        _line("ELECTRONICS", 50000.0),
        _line(False, 0.0),
    ],
)

DELIVERED_AT = datetime(2026, 10, 1, 11, 30)


def build_requests():
    """Every request body this connector sends, from the examples above."""
    api = services.floatra_api
    c = services.floatra_contract
    return {
        "request-onboard": api.onboard_body_for_partner(PARTNER),
        "request-initiate": api.initiate_body_for_order(ORDER),
        "request-confirm-delivery": c.confirm_delivery_body(
            delivered_at=DELIVERED_AT,
            notes="Delivered to the shop",
            agent_id="agt_01",
            agent_name="Tunde Bello",
            confirmation_method="ERP_CONSOLE",
            lat=6.6018,
            lng=3.3515,
            accuracy_meters=12,
            location_recorded_at=datetime(2026, 10, 1, 11, 29),
            proof_of_delivery_url="https://erp.example.com/pod/S00042.jpg",
        ),
        "request-confirm-delivery-partial": c.confirm_delivery_body(
            delivered_at=DELIVERED_AT,
            installment=1,
            delivered_amount=100000.0,
            undelivered_handling="DEFER_DELIVERY",
            defer_delivery_days=3,
        ),
        "request-cancel": c.cancel_body(
            reason_code="OTHER",
            reason_notes="Sale order S00042 cancelled in Odoo.",
        ),
        "request-repayment": c.repayment_body(
            amount=100000.0,
            reference="RCPT-0042",
            source="MANUAL_CASH",
            collected_at=datetime(2026, 10, 1, 11, 0),
            notes="Cash at the shop",
        ),
        "request-restructure": c.restructure_body(
            new_tenure_days="30",
            reason="Market closure after flooding",
            lender_approval_reference="LAR-2026-001",
            approved_by_label="ops@distributor.example",
        ),
        "request-bank-account": c.bank_account_body(
            bank_code="058",
            account_number="0123456789",
            account_name="Nkechi Okafor",
            bank_name="GTBank",
            is_primary=True,
        ),
    }


def write_request_fixtures():
    for name, body in build_requests().items():
        with open(os.path.join(FIXTURES, name + ".json"), "w", encoding="utf-8") as f:
            f.write(json.dumps(body, indent=2, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    write_request_fixtures()
