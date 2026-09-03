# Air Station — rewrite in progress (see README.md).
#
# Two sections:
#   OPERATOR COMMANDS — run on the Pi. Return in step 5 of the rewrite.
#   AGENT COMMANDS    — `agent-*`: used on the dev server, which has no Pi
#                       hardware and cannot reach the Pi.

.PHONY: help agent-venv agent-test agent-demo agent-demo-stop agent-clean

help: ## Show this help
	@grep -E '^[a-z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ==============================================================================
# AGENT COMMANDS (dev server; no Pi access from here)
# ==============================================================================

agent-venv: ## Dev virtualenv with test dependencies (no Pi hardware libs)
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

agent-test: ## Run the hardware-free test suite
	.venv/bin/python -m pytest tests/ -q

agent-demo: ## Run collector + manager + dashboard here with fake hardware (48 h seeded; Ctrl-C stops)
	.venv/bin/python tools/demo.py --reset --seed-hours 48

agent-demo-stop: ## Stop a demo started in the background
	@[ -f data/demo/demo.pid ] && kill $$(cat data/demo/demo.pid) && echo "demo stopped" || echo "no demo running"

agent-clean: ## Remove local venv, caches and imported data (dev server)
	rm -rf .venv from_pi
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
