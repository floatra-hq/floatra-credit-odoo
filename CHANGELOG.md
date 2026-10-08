# Changelog

All notable changes to the Floatra Odoo connector (`floatra_credit`).
Versions follow the addon's `__manifest__.py` (`<odoo series>.<major>.<minor>.<patch>`).

## [17.0.0.3.2] - 2026-10-08

- **Clear copy for a deactivated platform.** Floatra now answers a deactivated
  platform's API key with `403 PLATFORM_INACTIVE` (core, 2026-10-08); the
  connector says the platform is deactivated instead of the generic 403 text.

## [17.0.0.3.1] - 2026-10-08

Found by the staging pass against staging-api (2026-10-08).

- **Clear copy for a refused agent ID.** Confirming a delivery with an Agent ID
  Floatra does not know is refused with `403 AGENT_NOT_AUTHORIZED`; the
  connector showed the generic 403 text ("does not belong to the platform in
  the Floatra settings"), which sent staff to the wrong setting. It now says
  the agent ID is not recognised and how to proceed. `PLATFORM_SUSPENDED` gets
  its own message too.
- The partner's Floatra limit is labelled "Floatra Credit Limit", so it no
  longer shares a label with Odoo's own `credit_limit` (install warning).

## [17.0.0.3.0]

First public release, for Odoo 17.0. MIT licence.

- **Matches Floatra's partner API end to end** (#1302). Every call goes to
  `/v1/partner/...` on a host-only base URL (stored `/v1` and `api.floatra.io`
  values are migrated); responses are unwrapped from Floatra's envelope with
  its `errorCode`; money is sent as decimal-Naira strings. Onboarding stores
  the Floatra merchant id and never sends or stores BVN/NIN. Sale orders store
  the Floatra Loan id, which every lifecycle call uses. Webhooks verify the
  Unix-seconds signature, are anchored to the platform and realm (`livemode`),
  read flat payloads, and `order.funded` lifts the dispatch gate. KYC goes
  through Floatra's hosted link. Contract tests run against request, response
  and webhook fixtures rendered by Floatra core.
- **Installs on Odoo 17** (#1313). The sale-order status banners and the
  settings form are rewritten for Odoo 17's view rules; the Odoo runtime suite
  passes (57 tests).
- **A confirmed reorder lock is kept** (#1313). When a live lock-status refresh
  comes back locked, the request is refused with a notification and the lock is
  stored, so a retry is refused without calling Floatra again. An unreachable
  Floatra still refuses (fail-closed).
- **The financed amount is the tax-inclusive order total** (founder ruling
  2026-10-08): the loan covers the full invoice the merchant owes, VAT included.
