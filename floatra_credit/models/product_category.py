# -*- coding: utf-8 -*-
"""Floatra category mapping on Odoo's product.category.

Floatra prices credit by category (eligibility, tenure, interest
band, etc.). This selection lets the distributor map their own
internal product categories onto Floatra's enum. When a sale order
is submitted, the line items' categories are aggregated and the
dominant Floatra category is sent on the API payload.

Source of truth: the gateway's `OrderCategory` enum
(`prisma/schema.prisma`), which `/v1/orders/initiate` validates via
`@IsEnum(OrderCategory)`. The pre-P2-batch-6 list used a finer-
grained spec §5.1 style (FMCG_BEVERAGES, PHARMA_OTC, etc.) that
the gateway never accepted — every Sales Order with those values
returned 422. Realigned to the gateway enum so the dropdown can't
pick something the wire will reject; distributors describe their
internal categories via the human label and map them up to one of
the six coarse buckets.
"""

from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    # Keep in sync with prisma/schema.prisma::OrderCategory + the
    # `@IsEnum(OrderCategory)` validator on /v1/orders/initiate.
    # FMCG = Fast-Moving Consumer Goods; ELECTRONICS not financeable
    # at most tiers but kept here so the dropdown can flag it (the
    # spec §5.1 hint "(Not Eligible)" was load-bearing UX). OTHER is
    # the catch-all; partners with categories that don't fit one of
    # the named buckets should map to OTHER and rely on server-side
    # routing for eligibility.
    floatra_category = fields.Selection(
        [
            ("FMCG", "FMCG (Beverages, Food, Personal Care)"),
            ("ELECTRONICS", "Electronics (Limited Eligibility)"),
            ("FASHION", "Fashion / Apparel"),
            ("AGRICULTURE", "Agriculture"),
            ("PHARMACY", "Pharmacy"),
            ("OTHER", "Other (General Trade)"),
        ],
        string="Floatra Category",
        help="Maps this Odoo category to the Floatra category enum used "
        "for credit eligibility + risk routing. Choose the bucket that "
        "best matches your internal category; Floatra applies its own "
        "per-tier eligibility rules server-side. Leave blank to fall "
        "back to OTHER at order-submit time.",
    )
