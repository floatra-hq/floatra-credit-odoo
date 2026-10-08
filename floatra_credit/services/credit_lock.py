# -*- coding: utf-8 -*-
"""Reorder-lock enforcement helper.

Spec v3 §2.2 mandates a three-layer fallback for reorder locks:

  PRIMARY:    webhook delivery (merchant.reorder_locked)
  SECONDARY:  ERP polls GET /v1/merchants/{id}/lock-status before each order
  TERTIARY:   ERP caches lock status locally with 15-min TTL;
              stale cache defaults to LOCKED (fail-safe)

This module implements the tertiary layer in a way Odoo's stack
can use cheaply: read the `floatra_credit_status` field on
res.partner (kept fresh by the cron + webhooks), with a guard that
treats stale data as locked when the partner hasn't been refreshed
within the TTL.

A 15-min staleness check on `partner.write_date` isn't quite the
right signal (other writes touch the row too), so we track the
last credit-status sync via a dedicated `floatra_last_status_check`
column — populated by the credit-sync cron + every successful
get_lock_status call.

NOTE: this is a scaffold. The actual implementation lands once the
sale.order onchange / action_request_floatra_credit path is wired
end-to-end. For now the function shape exists so callers can write
to it.
"""

from datetime import datetime, timedelta

# Spec v3 §2.2 — 15-minute TTL. Beyond this, treat the cached
# status as unreliable and default to LOCKED (fail-safe).
STATUS_CACHE_TTL = timedelta(minutes=15)


def is_locked_failsafe(partner, now: datetime | None = None) -> bool:
    """Return True if the partner is locked OR the cache is stale.

    Used by sale.order.action_request_floatra_credit + action_confirm
    to refuse to ship goods when we can't prove the merchant is
    eligible. "Can't prove" includes "haven't heard from Floatra
    recently enough" — fail-safe.

    Args:
        partner: a res.partner record with floatra_credit_status +
            floatra_last_status_check.
        now: optional override for testability.

    Returns True if any of:
      - explicit lock: floatra_credit_status in {locked, suspended}
      - never-synced 'available': claims `available` but
        floatra_last_status_check is unset (we don't trust an
        unverified 'available' value)
      - stale 'available': floatra_last_status_check older than
        STATUS_CACHE_TTL (15 min). Refresh via the 4h sync cron or
        an explicit get_lock_status call.
    """
    if not now:
        now = datetime.utcnow()
    if partner.floatra_credit_status in ("locked", "suspended"):
        return True
    if partner.floatra_credit_status == "available":
        last = partner.floatra_last_status_check
        if not last:
            return True
        if isinstance(last, datetime) and now - last > STATUS_CACHE_TTL:
            return True
    return False
