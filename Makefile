# Floatra Odoo Connector — dev convenience targets.
#
# Standard flow on a fresh checkout:
#
#   1. make up        — start the Odoo + Postgres containers
#   2. make init      — install the addon into a fresh database
#                       (only the first time; `up` alone is enough
#                       after that)
#   3. Open http://127.0.0.1:8069 → log in admin / admin
#
# After code changes:
#
#   make upgrade      — reload the addon (picks up Python + XML edits)
#   make test         — run the addon's own test suite (in Odoo)
#   make plain-test   — contract + client tests, no Odoo/Docker needed
#
# Cleanup:
#
#   make down         — stop containers (data persisted in volumes)
#   make clean        — stop + nuke volumes (loses DB + filestore)

COMPOSE := docker compose -f docker-compose.yml
DB := floatra_dev
ADDON := floatra_credit

.PHONY: up down restart logs init upgrade test plain-test shell psql clean

## --- Lifecycle ---

up:  ## Start Odoo + Postgres in the background.
	$(COMPOSE) up -d
	@echo "Odoo coming up at http://127.0.0.1:8069 (give it ~15s on first start)"

down:  ## Stop containers; volumes preserved.
	$(COMPOSE) down

restart:  ## Restart Odoo only (Postgres keeps running).
	$(COMPOSE) restart odoo

logs:  ## Tail Odoo logs.
	$(COMPOSE) logs -f odoo

## --- Addon ---

init:  ## Install floatra_credit into a fresh $(DB).
	$(COMPOSE) run --rm odoo \
	  odoo --config=/etc/odoo/odoo.conf \
	       -d $(DB) -i $(ADDON) --stop-after-init

upgrade:  ## Re-load floatra_credit code into the running DB.
	$(COMPOSE) exec odoo \
	  odoo --config=/etc/odoo/odoo.conf \
	       -d $(DB) -u $(ADDON) --stop-after-init
	$(MAKE) restart

test:  ## Run the addon's test suite against $(DB).
	$(COMPOSE) run --rm odoo \
	  odoo --config=/etc/odoo/odoo.conf \
	       -d $(DB)_test -i $(ADDON) \
	       --test-enable --test-tags $(ADDON) --stop-after-init

plain-test:  ## Contract + client tests against core-rendered fixtures (no Odoo).
	cd $(ADDON)/tests && python3 -m unittest \
	  test_contract test_kyc_link test_floatra_api_redaction test_kyc_link_access_static

## --- Debugging ---

shell:  ## Drop into Odoo's Python shell on $(DB).
	$(COMPOSE) exec odoo \
	  odoo shell --config=/etc/odoo/odoo.conf -d $(DB)

psql:  ## Open psql on $(DB).
	$(COMPOSE) exec db psql -U odoo -d $(DB)

## --- Cleanup ---

clean:  ## Stop containers AND delete all data volumes (destructive).
	$(COMPOSE) down -v
