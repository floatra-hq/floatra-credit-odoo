# Floatra Odoo Connector

`floatra_credit/` — native Odoo addon that embeds Floatra inventory financing into the standard sales-order flow.

## Status

Every call the addon makes and every webhook it handles is checked against
Floatra core's real contract (17.0.0.3.0): core renders the request/response
pairs and signed webhook deliveries from its own controllers, DTO validation,
exception filter and webhook pipeline into `floatra_credit/tests/fixtures/contract/`,
and core validates this addon's request bodies against its DTOs. The
plain-Python suite runs the addon's client, parsers and webhook routing
against those files.

What it does:

- **Onboard with Floatra** on a customer → `POST /v1/partner/merchants/onboard`
  (Floatra merchant id stored on the partner; no BVN/NIN is sent or stored)
- **Get KYC Link** → hosted KYC link (see below)
- **Request Financing** on a sale order → `POST /v1/partner/orders/initiate`
  (decimal-Naira `amount`); stores Floatra's Order id AND Loan id
- `action_confirm` **blocks dispatch** for a reorder-locked / suspended
  customer, and while credit is pending / approved / waitlisted but not funded
- Delivery confirmation (full or partial), cash repayment, restructure,
  bank-account wizards → the `/v1/partner/orders/:loan_id/...` and
  `/v1/partner/merchants/:id/bank-account` routes
- Cancelling an approved, unfunded order releases Floatra's reservation
  (`POST /v1/partner/orders/:loan_id/cancel`)
- Webhook receiver at `/floatra/webhook`: HMAC-SHA256 over
  `"<X-Floatra-Timestamp>.<raw body>"` (Unix-seconds timestamp, 5-minute
  window), dedup on `X-Floatra-Event-ID`, flat camelCase payloads; handles
  every event core sends a platform (`merchant.*`, `order.*`,
  `bulk.order_decision`, `platform.*`, `vendor.suspension_requested`)
- Crons: hourly credit-status sync; daily undelivered-webhook replay +
  acknowledge
- Fail-safe lock guard: a stale `available` (older than 15 min) counts as
  locked until a live lock-status refresh clears it

Known limitations:

- The Odoo-runtime suite (`tests/test_floatra_credit.py`) needs an Odoo 17
  database; run it (`make test`) before the first distributor install.
- `platform.suspended` sets `floatra.platform_suspended=true`, which blocks
  new credit requests; Floatra sends no "reinstated" event, so clear the
  system parameter by hand once Floatra confirms reinstatement.
- Requesting credit again on a declined order returns the same decision
  (Floatra deduplicates on the order reference); use a new sale order.
- Returns are not offered: Floatra takes a return only against a loan with
  an open delivery dispute, which online orders cannot have yet.

Verification you can run locally before deploying: see [Local dev environment](#local-dev-environment) below.

## What changes hands during onboarding

This is where the integration gets a lot less scary than it sounds. **Floatra never gets your Odoo password.** The connector calls Floatra's API outbound; Floatra calls back via signed webhooks to a URL you expose.

| Who | Gets | From whom | Stored where |
|---|---|---|---|
| Distributor | Floatra API key (`live_pk_*`; sandbox: `sbx_pk_*`) | Floatra account team | Odoo `Settings → Floatra` |
| Distributor | Floatra webhook secret (HMAC) | Floatra account team | Odoo `Settings → Floatra` |
| Distributor | Floatra Platform ID (slug) | Floatra account team | Odoo `Settings → Floatra` |
| Floatra | Distributor's webhook URL | Distributor (e.g. `https://erp.acme.ng/floatra/webhook`) | Floatra `Platform.webhookUrl` |

That's it. **No Odoo credentials are shared with Floatra** — the connection is:

- **Odoo → Floatra** (outbound HTTP, authenticated by the Bearer token in Floatra's API key)
- **Floatra → Odoo** (outbound HTTP from Floatra to your public webhook URL, authenticated by HMAC-SHA256 over the request body using the shared webhook secret)

The Floatra-side platform record stores your `webhookUrl` + an `apiKeyHash` (not the raw key) + the `signatureSecret`. None of those are Odoo credentials.

## Production install (distributors)

You install this addon inside your own Odoo instance. Floatra does not touch your Odoo.

1. **Get the addon** (Odoo 17.0) from the read-only mirror
   [`floatra-hq/floatra-credit-odoo`](https://github.com/floatra-hq/floatra-credit-odoo).
   Only install from `github.com/floatra-hq`; anything under another owner is
   not from Floatra. Either:
   - **Release zip** (recommended): download `floatra_credit-odoo-<version>.zip`
     and its `.sha256` from the
     [Releases page](https://github.com/floatra-hq/floatra-credit-odoo/releases),
     verify, and unzip into a directory on your `addons_path`:

     ```bash
     shasum -a 256 -c floatra_credit-odoo-17.0.0.3.0.zip.sha256   # macOS
     sha256sum -c floatra_credit-odoo-17.0.0.3.0.zip.sha256       # Linux
     unzip floatra_credit-odoo-17.0.0.3.0.zip -d /path/to/addons   # -> addons/floatra_credit/
     ```
   - **Git**: clone the release tag and add the clone to your `addons_path`
     (the addon is the `floatra_credit/` directory inside it):

     ```bash
     git clone --depth 1 --branch v17.0.0.3.0 https://github.com/floatra-hq/floatra-credit-odoo
     ```

   **Apps → Import Module does not work for this addon**: that importer
   (`base_import_module`) loads data-only modules and skips Python code, which
   this addon needs. It must be on the server's `addons_path`, so it cannot be
   installed on Odoo Online (SaaS).
2. **Restart Odoo**, then **Apps → Update Apps List**, search "Floatra" and
   **Install** (or start Odoo once with `-i floatra_credit`; upgrades use
   `-u floatra_credit`).
3. **As an Odoo admin:** open `Settings → Floatra` and paste the four values your Floatra account team gave you:
   - **API Base URL** — `https://api.floatra.com` (host only; the addon adds `/v1/partner/...`). The same host serves live and sandbox: the API key decides which. An old `https://api.floatra.io/v1` value is migrated on upgrade.
   - **Platform ID** — your slug (e.g. `acme-distributors`)
   - **API Key** — the `live_pk_*` Bearer token (sandbox keys start `sbx_pk_`, or `pk_` after a rotation)
   - **Webhook Secret** — the HMAC signing secret
4. **Tell Floatra your webhook URL** — `https://<your-odoo-host>/floatra/webhook`. Floatra writes it to your `Platform.webhookUrl` record on their side. This URL needs to be publicly reachable; if your Odoo runs behind a firewall, the easiest path is to put it behind a public reverse proxy or use a tunneling service (e.g. Cloudflare Tunnel, ngrok) during pilot.
5. **As an Odoo sales user:** open a customer record (it needs a Nigerian mobile number) → **Onboard with Floatra**. The Floatra Merchant ID appears on the **Floatra** tab; then **Get KYC Link** and send it to the merchant. Credit status refreshes hourly (or via `Settings → Technical → Scheduled Actions → Floatra: Sync Credit Status`) and immediately on Floatra's webhooks.
6. **First credit request:** open a sales order for that customer → click **Request Floatra** in the header.

If anything in the API or webhook path is broken, the **Floatra → Webhook Logs** menu shows every inbound event with HMAC signature, payload, and processing status.

## Local dev environment

Self-contained Docker Compose setup so you don't need a system-wide Odoo install. **Requires Docker Desktop (or compatible) on the host.**

```bash
cd connectors/odoo
make up        # start Odoo 17 + Postgres 15 in the background
make init      # install floatra_credit into a fresh `floatra_dev` database
# → open http://127.0.0.1:8069
# → log in as admin / admin
```

Common loops:

| Task | Command |
|---|---|
| Tail Odoo logs | `make logs` |
| Restart Odoo only | `make restart` |
| Reload the addon after Python/XML edits | `make upgrade` |
| Run the test suite (separate test DB) | `make test` |
| Drop into Odoo's Python shell | `make shell` |
| Open `psql` against the dev DB | `make psql` |
| Stop containers (data preserved) | `make down` |
| Nuke containers + volumes | `make clean` (destructive) |

The addon directory is **volume-mounted**, so Python edits hot-reload on Odoo's `--dev=all` mode. XML view edits require `make upgrade` to re-load.

The dev environment binds only to `127.0.0.1:8069` and Postgres is not exposed at all — both stay inside the compose network.

## Run the tests outside Docker

If you have Odoo installed natively:

```bash
odoo-bin -i floatra_credit -d test_db --test-enable --stop-after-init --test-tags floatra_credit
```

The contract and client tests need no Odoo runtime (plain Python 3.10+ with
`requests`; pytest also works), from `floatra_credit/tests/`:

```bash
python3 -m unittest test_contract test_kyc_link test_floatra_api_redaction test_kyc_link_access_static
# or: make plain-test   (from connectors/odoo)
```

`tests/fixtures/contract/` holds the responses and signed webhook
deliveries rendered by Floatra core's contract test
(`src/modules/partner-webhooks/partner-contract-fixtures.spec.ts`;
regenerate there with `UPDATE_CONTRACT_FIXTURES=1`). The `request-*` files
are this connector's request bodies (`tests/contract_support.py` writes
them: `python3 tests/contract_support.py`), which core validates against
its DTOs.

## Identity verification (KYC): hosted link

Production KYC is a selfie and liveness check on the merchant's own phone,
so the connector never sends a BVN, NIN or photo. On an onboarded partner,
**Get KYC Link** calls `POST /v1/partner/merchants/:id/kyc/retry` with an
empty body. Floatra returns a single-use link valid for 24 hours, which is
stored on the partner (Floatra tab → KYC: copy button + expiry). Send it to
the merchant by SMS or WhatsApp, or with **Email KYC Link** (Odoo's email
composer, shown while the link is unexpired and the partner has an email).
The merchant confirms a code sent to their phone number on Floatra, enters
their NIN or BVN and takes the selfie; the result arrives as the
`merchant.kyc_completed` webhook, which also clears the stored link.
Getting a new link revokes the previous one. Floatra allows 5 links per
merchant and 200 per platform an hour.

Refusals are shown as plain messages: already verified (409
`KYC_ALREADY_VERIFIED`), not available (409 `NOT_AVAILABLE`), too many links
(429), KYC temporarily off (503), unknown merchant (404), another platform's
merchant (403), wrong environment's key (422).

The link fields, both buttons and the Floatra tab are limited to the
**Floatra / User** group (`floatra_credit.group_floatra_user`); the actions
raise AccessError for anyone else, including RPC callers.

With a **sandbox** key (any key that does not start `live_pk_`: `sbx_pk_…`,
or `pk_…` after a rotation) Floatra returns a simulated result
instead of a link; the connector sends a fixed test body (dummy identifier,
1x1 placeholder image) and mirrors the result onto the KYC status.

## Changelog

- **17.0.0.3.0 (2026-10-07):** Every call and webhook matches Floatra's
  current partner API, contract-tested against core-rendered fixtures.
  Base URL is host-only (`https://api.floatra.com`; a stored `…/v1` or the
  old `api.floatra.io` default is migrated). All calls use
  `/v1/partner/...`, unwrap Floatra's `{success, data, …}` envelope, map
  errors by `errorCode`, and send money as decimal-Naira strings (no
  `amount_kobo`, no `is_test`). Onboarding works and stores the Floatra
  merchant id; it no longer sends BVN/NIN, and the BVN/NIN partner fields
  are removed (columns dropped on upgrade). Sale orders store Floatra's Loan
  id (`floatra_loan_id`, looked up by order reference for older orders) and
  every lifecycle call uses it. Webhooks verify the Unix-seconds timestamp
  signature and read core's flat camelCase payloads; `order.funded` (lender
  track) now lifts the dispatch gate like `order.disbursed`. Partial
  deliveries send the installment; cancelling an approved, unfunded order
  releases the reservation. Removed: the test-mode setting, the 2-hour
  approval-expiry cron (Floatra retired the window), the lender-name and
  approval-expiry order fields. Upgrade with `-u floatra_credit`.
  Webhook safety: events whose `livemode` does not match the configured
  key are logged and ignored (a live key also ignores unmarked events);
  records are matched by Floatra loan / loan-request / merchant ids first,
  and an ERP-local order or customer name is accepted only when its stored
  Floatra ids agree. Only successfully applied deliveries are deduplicated
  (a failed one is retried, inside a savepoint), and a signature already
  applied is not applied again under a new event id.
  Onboarding and Request Financing require the Floatra user group (like
  the KYC-link actions). Onboarding sends `owner.email` only when it is a
  single valid address. The bank-account Idempotency-Key no longer carries
  the account number.
- **17.0.0.2.0 (2026-10-07):** KYC uses Floatra's hosted link. "Retry KYC"
  (which sent the partner's BVN/NIN, refused in production with
  `400 KYC_HOSTED_LINK_ONLY`) is replaced by **Get KYC Link** and **Email
  KYC Link**; new partner fields `floatra_kyc_url`,
  `floatra_kyc_url_expires_at`. The button no longer needs a BVN/NIN on the
  partner. `merchant.kyc_completed` clears the stored link. Adds `mail` to
  the module dependencies. Upgrade the module (`-u floatra_credit`) to add
  the fields.

## Repository layout

This addon will eventually move to its own repo (`floatra-odoo-connector`) so it can release independently to the Odoo App Store. Lives under `connectors/odoo/` in the Floatra monorepo until then.
