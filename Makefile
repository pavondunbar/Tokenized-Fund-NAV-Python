# =========================================================================
# Tokenized Fund / NAV System — Makefile
# =========================================================================
# Usage:
#   make up           — Build and start all services
#   make down         — Stop and remove all containers
#   make demo         — Rebuild and run the full demo lifecycle
#   make logs         — Tail all container logs
#   make db-balances  — Query derived investor balances from the ledger
#   make test         — Run unit tests (no Docker required)
# =========================================================================

.PHONY: up down demo logs db-balances test clean status db-shell

# -------------------------------------------------------------------
# Core commands
# -------------------------------------------------------------------

up:
	docker compose up --build -d
	@echo ""
	@echo "All services starting. Run 'make logs' to follow output."

down:
	docker compose down -v --remove-orphans

demo:
	docker compose down -v --remove-orphans
	docker compose up --build --abort-on-container-exit fund-service

logs:
	docker compose logs -f

# -------------------------------------------------------------------
# Database inspection
# -------------------------------------------------------------------

DB_CONTAINER := fund-ledger-db
DB_CMD := docker exec -t $(DB_CONTAINER) psql -U ledger_user -d fund_ledger

db-balances:
	@$(DB_CMD) -c "\echo '--- Investor Share Balances ---'" \
		-c "SELECT i.investor_name, isb.share_balance, isb.cost_basis \
		    FROM investor_share_balances isb \
		    JOIN investors i ON i.id = isb.investor_id \
		    WHERE i.investor_name NOT LIKE 'TREASURY/%' \
		    ORDER BY isb.share_balance DESC;" \
		-c "\echo ''" \
		-c "\echo '--- Fund Shares Outstanding ---'" \
		-c "SELECT f.fund_name, fso.total_shares \
		    FROM fund_shares_outstanding fso \
		    JOIN funds f ON f.id = fso.fund_id;" \
		-c "\echo ''" \
		-c "\echo '--- Cash Journal Totals ---'" \
		-c "SELECT journal_id, \
		           SUM(debit_amount) AS total_debit, \
		           SUM(credit_amount) AS total_credit \
		    FROM cash_ledger \
		    GROUP BY journal_id \
		    ORDER BY journal_id \
		    LIMIT 10;"

db-shell:
	docker exec -it $(DB_CONTAINER) psql -U ledger_user -d fund_ledger

# -------------------------------------------------------------------
# Tests (run locally, no Docker required)
# -------------------------------------------------------------------

VENV := .venv

$(VENV):
	uv venv $(VENV) --python 3.13
	uv pip install pytest --python $(VENV)/bin/python

test: $(VENV)
	$(VENV)/bin/python -m pytest tests/ -q

# -------------------------------------------------------------------
# Utilities
# -------------------------------------------------------------------

status:
	docker compose ps

clean: down
	docker volume prune -f
