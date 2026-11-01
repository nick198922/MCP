.PHONY: install test test-slow lint fmt smoke run run-http

install:
	uv sync --extra dev

# Tests marked `slow` are skipped here; CI runs plain pytest (all tests), `make test-slow` runs only them.
test:
	uv run --extra dev pytest -q -n auto -m "not slow"

test-slow:
	uv run --extra dev pytest -q -m slow

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff format .
	uv run ruff check --fix .

# Live check against the real exchanges. Run on a machine with internet access to them.
smoke:
	uv run python scripts/live_smoke.py

run:
	uv run tradedesk-mcp

run-http:
	TRADEDESK_TRANSPORT=streamable-http TRADEDESK_HOST=127.0.0.1 TRADEDESK_PORT=8000 uv run tradedesk-mcp
