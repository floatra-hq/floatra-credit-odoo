# -*- coding: utf-8 -*-
{
    "name": "Floatra",
    "version": "17.0.0.3.1",
    "summary": "Embedded inventory credit for Nigerian distributors",
    "description": """
Floatra — Native Odoo Connector
=======================================

Adds Floatra-funded inventory credit to the standard Odoo sales-order
flow. When a customer marked as a Floatra merchant places an order, the
sales user can request financing; on approval, dispatch is
gated on Floatra disbursement, and the repayment lifecycle is tracked
via webhooks back from Floatra.

Distributor never sees a Floatra UI — everything happens inside Odoo.
All credit decisions, fund movements, and enforcement are made by
Floatra's API; this module is purely the integration layer.

Setup: see README.md for configuration steps. You need a Floatra API
key + platform ID + webhook secret from your Floatra account manager.
""",
    "author": "Floatra",
    "website": "https://floatra.com",
    # MIT. Odoo's license field is a fixed selection with no "MIT" value;
    # MIT is OSI-approved, so this is the matching choice. Text: LICENSE.
    "license": "Other OSI approved licence",
    "category": "Sales/Credit",
    "depends": [
        "base",
        "sale_management",
        "product",
        "contacts",
        # Email KYC Link opens mail.compose.message (contacts already
        # pulls mail in; declared explicitly because we use it).
        "mail",
    ],
    # Order matters: data files (security, then views, then cron) load
    # in the order listed. Security CSV is loaded BEFORE the model
    # definitions need its access rules.
    "data": [
        "security/floatra_security.xml",
        "security/ir.model.access.csv",
        "views/floatra_menu.xml",
        "views/res_partner_views.xml",
        "views/bank_account_wizard_views.xml",
        "views/sale_order_views.xml",
        "views/restructure_wizard_views.xml",
        "views/product_category_views.xml",
        "views/res_config_settings_views.xml",
        "data/floatra_cron.xml",
    ],
    "images": ["static/description/icon.png"],
    "installable": True,
    "application": False,
    "auto_install": False,
}
