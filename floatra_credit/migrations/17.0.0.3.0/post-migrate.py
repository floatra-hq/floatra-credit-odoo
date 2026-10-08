# -*- coding: utf-8 -*-
"""17.0.0.3.0: align stored settings and data with the current Floatra API.

* ``floatra_credit.api_base_url`` becomes host-only
  (``https://api.floatra.com``): a trailing ``/v1`` is stripped and the old
  ``*.floatra.io`` default, which never resolved, is replaced.
* The test-mode setting is deleted (Floatra rejects ``is_test``; use the
  sandbox API key to test).
* Retired sale-order columns are dropped: ``floatra_is_test``,
  ``floatra_lender_name`` (core never sends a lender name) and
  ``floatra_approval_expires_at`` (Floatra retired the 2-hour approval
  window).
* The BVN / NIN columns on ``res.partner`` are dropped: Floatra ignores
  identity numbers at onboarding and KYC runs on the hosted link, so the
  addon no longer stores them.

Sale orders created before this version only carry the Floatra Order id;
the Loan id is looked up by order reference the first time it is needed
(``sale.order._floatra_loan_id_or_raise``).
"""

import logging

from odoo.addons.floatra_credit.services.floatra_contract import normalize_base_url

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute(
        "SELECT value FROM ir_config_parameter WHERE key = %s",
        ("floatra_credit.api_base_url",),
    )
    row = cr.fetchone()
    if row:
        new_value = normalize_base_url(row[0])
        if new_value != row[0]:
            cr.execute(
                "UPDATE ir_config_parameter SET value = %s WHERE key = %s",
                (new_value, "floatra_credit.api_base_url"),
            )
            _logger.info("Floatra base URL migrated to %s", new_value)
    cr.execute(
        "DELETE FROM ir_config_parameter WHERE key = %s",
        ("floatra_credit.test_mode",),
    )
    cr.execute(
        "ALTER TABLE res_partner DROP COLUMN IF EXISTS floatra_bvn, "
        "DROP COLUMN IF EXISTS floatra_nin"
    )
    cr.execute(
        "ALTER TABLE sale_order DROP COLUMN IF EXISTS floatra_is_test, "
        "DROP COLUMN IF EXISTS floatra_lender_name, "
        "DROP COLUMN IF EXISTS floatra_approval_expires_at"
    )
