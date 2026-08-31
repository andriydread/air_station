# Air Monitor.
#
# Two sections:
#   USER COMMANDS  — run on the Pi from ~/air_station. Workflow:
#                    git pull, then `make deploy`. First time: `make init`.
#   AGENT COMMANDS — `agent-*`: used by the coding agent on the dev server.
#                    The dev server can NOT reach the Pi (it lives on the
#                    operator's home LAN), so nothing here talks to it —
#                    data travels Pi -> server via `make push-data`.

SERVICES = airmonitor.service airmonitor-web.service
UNIT_FILES = /etc/systemd/system/airmonitor.service \
             /etc/systemd/system/airmonitor-web.service \
             /etc/systemd/system/wifi-powersave-off.service

.PHONY: help init deploy restart push-data delete-all delete-venv delete-service delete-data _pi \
        agent-test agent-venv agent-clean

help: ## Show this help
	@grep -E '^[a-z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ==============================================================================
# USER COMMANDS (on the Pi)
# ==============================================================================

_pi:  # refuse to run the Pi commands anywhere else
	@[ -d /etc/systemd/system ] && [ "$$(whoami)" = "pi" ] || \
		{ echo "This command runs ON the Pi (as user pi). Agent commands are the agent-* ones."; exit 1; }

init: _pi ## Full clean install: fresh venv, requirements, services, watchdog
	rm -rf .venv
	python3 -m venv .venv
	sh systemd/enable-watchdog.sh
	@$(MAKE) --no-print-directory deploy
	@echo ""
	@echo "Init complete. Reboot once (sudo reboot) to arm the hardware watchdog."

deploy: _pi ## Install requirements + service files (new or updated), (re)start all
	.venv/bin/pip install -q -r requirements.txt
	mkdir -p data/logs
	sudo cp systemd/*.service /etc/systemd/system/
	sudo install -m 440 systemd/airmonitor-sudoers /etc/sudoers.d/airmonitor
	sudo visudo -c -q
	sudo systemctl daemon-reload
	sudo systemctl enable wifi-powersave-off.service $(SERVICES)
	sudo systemctl restart wifi-powersave-off.service $(SERVICES)
	@echo "--- deployed; services:"
	@systemctl is-active $(SERVICES) || true
	@echo "--- wifi power save (want: off):"
	@iw dev wlan0 get power_save 2>/dev/null || true

restart: _pi ## Restart the app services
	sudo systemctl restart $(SERVICES)
	@systemctl is-active $(SERVICES) || true

push-data: _pi ## Upload database + logs to the dev server for tuning (DEST=user@host)
	@[ -n "$(DEST)" ] || { echo "Usage: make push-data DEST=user@host"; exit 1; }
	ssh $(DEST) "mkdir -p air_station/from_pi"
	scp -r data $(DEST):air_station/from_pi/

delete-all: _pi ## Delete services + venv + caches; then offers data deletion
	@$(MAKE) --no-print-directory delete-service
	@$(MAKE) --no-print-directory delete-venv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	-@$(MAKE) --no-print-directory delete-data

delete-venv: ## Delete the virtualenv
	rm -rf .venv

delete-service: _pi ## Stop, disable and remove service files + sudoers
	sudo systemctl disable --now $(SERVICES) wifi-powersave-off.service 2>/dev/null || true
	sudo rm -f $(UNIT_FILES) /etc/sudoers.d/airmonitor
	sudo systemctl daemon-reload
	sudo systemctl reset-failed 2>/dev/null || true
	@echo "Services removed."

delete-data: _pi ## Delete ALL stored data — asks for confirmation (FORCE=1 skips)
	@[ "$(FORCE)" = "1" ] || { printf "Delete ALL stored data (database + logs)? [y/N] "; \
		read a; [ "$$a" = "y" ] || { echo "Kept."; exit 1; }; }
	sudo systemctl stop $(SERVICES) 2>/dev/null || true
	rm -rf data
	@echo "Data deleted. 'make restart' starts fresh (services must still be installed)."

# ==============================================================================
# AGENT COMMANDS (dev server; no Pi access from here)
# ==============================================================================

agent-test: ## Run the hardware-free test suite
	.venv/bin/python -m pytest tests/ -q

agent-venv: ## Dev virtualenv with test dependencies (no Pi hardware libs)
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

agent-clean: ## Remove local venv, caches and pushed data (dev server)
	rm -rf .venv from_pi
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete
