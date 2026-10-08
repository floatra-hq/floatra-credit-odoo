# -*- coding: utf-8 -*-
"""Odoo side of the Floatra client: settings → client, records → request bodies.

The HTTP client (``floatra_client.FloatraClient``) and the wire contract
(``floatra_contract``) are shared with the ERPNext app; this module only
knows Odoo records. It has no ``odoo`` import, so the record mappers are
unit-tested with plain stand-in objects.

Identifiers sent to Floatra:

* ``external_merchant_id`` = ``str(res.partner.id)`` (stable; set at
  onboarding, sent again on every order);
* ``external_order_id`` = ``sale.order.name`` (the order reference);
* lifecycle calls (confirm-delivery, cancel, repayments, restructure) use the
  Floatra LOAN id (``sale.order.floatra_loan_id``), not the Order id.
"""

from decimal import Decimal

from . import floatra_contract as contract
from .floatra_client import FloatraClient
from .floatra_contract import ContractError, FloatraAPIError  # noqa: F401

ERP_TYPE = "ODOO"

__all__ = [
    "ContractError",
    "FloatraAPIClient",
    "FloatraAPIError",
    "initiate_body_for_order",
    "odoo_values",
    "onboard_body_for_partner",
    "resolve_floatra_category",
]


class FloatraAPIClient(FloatraClient):
    """``FloatraClient`` configured from Settings → Floatra."""

    @classmethod
    def for_env(cls, env) -> "FloatraAPIClient":
        config = env["res.config.settings"].sudo().get_floatra_config()
        return cls(
            base_url=config["base_url"],
            api_key=config["api_key"],
            platform_id=config["platform_id"],
        )


def external_merchant_id_for(partner) -> str:
    """The id Floatra knows this partner by (``MerchantPlatformLink``)."""
    return str(partner.id)


def onboard_body_for_partner(partner) -> dict:
    """``res.partner`` → ``POST /v1/partner/merchants/onboard`` body.

    Odoo's ``city`` stands in for the LGA (the NG localisation stores the LGA
    there for kiosk-scale partners).
    """
    state = getattr(partner, "state_id", None)
    return contract.onboard_body(
        external_merchant_id=external_merchant_id_for(partner),
        name=getattr(partner, "name", None),
        phone=getattr(partner, "mobile", None) or getattr(partner, "phone", None),
        business_type=getattr(partner, "floatra_business_type", None),
        category=getattr(partner, "floatra_primary_category", None),
        assigned_agent_id=getattr(partner, "floatra_assigned_agent_id", None),
        owner_first_name=getattr(partner, "floatra_owner_first_name", None),
        owner_last_name=getattr(partner, "floatra_owner_last_name", None),
        owner_email=getattr(partner, "email", None),
        street=getattr(partner, "street", None),
        lga=getattr(partner, "city", None),
        state=getattr(state, "name", None) if state else None,
    )


def resolve_floatra_category(order) -> str:
    """The Floatra category carrying the most revenue on the order (else OTHER)."""
    weights: dict = {}
    for line in order.order_line:
        category = line.product_id.categ_id
        key = getattr(category, "floatra_category", None) if category else None
        if not key:
            continue
        weights[key] = weights.get(key, Decimal(0)) + contract.to_decimal(
            line.price_subtotal or 0,
        )
    if not weights:
        return "OTHER"
    return max(weights.items(), key=lambda kv: kv[1])[0]


def initiate_body_for_order(order) -> dict:
    """``sale.order`` → ``POST /v1/partner/orders/initiate`` body."""
    return contract.initiate_body(
        external_merchant_id=external_merchant_id_for(order.partner_id),
        external_order_id=order.name,
        # Tax-inclusive by design (founder ruling 2026-10-08): the loan finances
        # the full invoice the merchant owes, VAT included, for VAT-registered
        # distributors too. Do not switch to the untaxed amount.
        amount=order.amount_total,
        category=resolve_floatra_category(order),
        tenure_days=getattr(order, "floatra_tenure_days", None),
        erp_type=ERP_TYPE,
    )


def odoo_values(updates: dict, field_names) -> dict:
    """Contract updates → an Odoo ``write`` dict.

    Keeps only the fields this model has (the contract names fields shared
    with the ERPNext app) and turns ``None`` (clear) into Odoo's ``False``.
    """
    return {
        name: (False if value is None else value)
        for name, value in updates.items()
        if name in field_names
    }
