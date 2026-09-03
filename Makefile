# Air Station — three programs (collector / manager / dashboard) on one Pi.
#
# Two sections:
#   OPERATOR COMMANDS — run on the Pi from the checkout. First time: `make init`
#                       (then reboot once). Updates: `git pull && make deploy`.
#   AGENT COMMANDS    — `agent-*`: used on the dev server, which has no Pi
#                       hardware and cannot reach the Pi.

UNITS      = airstation-collector airstation-manager airstation-dashboard
ALL_UNITS  = wifi-powersave-off $(UNITS)
USER_NAME  = $(shell whoami)
REPO       = $(CURDIR)
RENDER     = sed -e 's|@USER@|$(USER_NAME)|g' -e 's|@REPO@|$(REPO)|g'
DB         = data/airstation.db

.PHONY: help init deploy restart status logs recovery delete-data _pi _hardware _apt _venv _pip _units _watchdog \
        agent-venv agent-test agent-demo agent-demo-stop agent-clean

help: ## Show this help
	@grep -E '^[a-z_-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-16s %s\n", $$1, $$2}'

# ==============================================================================
# OPERATOR COMMANDS (on the Pi)
# ==============================================================================

_pi:  # refuse to run the Pi commands anywhere else
	@[ -d /etc/systemd/system ] && [ -e /dev/i2c-1 ] || \
		{ echo "This command runs ON the Pi (systemd + /dev/i2c-1). On the dev server use the agent-* commands."; exit 1; }

_hardware: _pi  # I2C and SPI are enabled by hand (raspi-config) before make init
	@[ -e /dev/i2c-1 ] && [ -e /dev/spidev0.0 ] || \
		{ echo "I2C or SPI is off: sudo raspi-config → Interface Options → enable I2C and SPI, reboot, then make init again."; exit 1; }

_apt: _pi
	sudo apt-get install -y python3-venv python3-dev swig liblgpio-dev

_venv: _pi
	[ -x .venv/bin/python ] || python3 -m venv .venv

_pip: _pi  # progress visible: the first install on a fresh card may compile for a while
	.venv/bin/pip install -r requirements.txt
	mkdir -p data/logs

_units: _pi  # render the templates with this user and this checkout; sudoers is checked before it lands
	@for unit in $(UNITS); do \
		$(RENDER) systemd/$$unit.service.in | sudo tee /etc/systemd/system/$$unit.service >/dev/null; \
		echo "installed /etc/systemd/system/$$unit.service"; \
	done
	sudo cp systemd/wifi-powersave-off.service /etc/systemd/system/wifi-powersave-off.service
	@$(RENDER) systemd/airstation-sudoers.in > /tmp/airstation-sudoers && \
		sudo visudo -c -q -f /tmp/airstation-sudoers && \
		sudo install -m 440 -o root -g root /tmp/airstation-sudoers /etc/sudoers.d/airstation && \
		rm -f /tmp/airstation-sudoers && echo "installed /etc/sudoers.d/airstation"
	sudo systemctl daemon-reload

_watchdog: _pi
	sh systemd/enable-watchdog.sh

init: _hardware _apt _venv _pip _units _watchdog ## Fresh Pi: packages, venv, units, sudoers, watchdog; then reboot once
	sudo systemctl enable --now $(ALL_UNITS)
	@echo ""
	@systemctl is-active $(UNITS) || true
	@echo ""
	@echo "Init complete. Reboot once (sudo reboot) to arm the hardware watchdog."
	@echo "Then: make status — and the first-hour checklist in README.md."

deploy: _pip _units ## After git pull: requirements, unit files, sudoers; restart the three apps
	sudo systemctl restart $(UNITS)
	@echo "--- deployed; units:"
	@systemctl is-active $(UNITS) || true

restart: _pi ## Restart the three apps (after editing config.toml)
	sudo systemctl restart $(UNITS)
	@systemctl is-active $(UNITS) || true

status: _pi ## One screen: units, data ages, database, disk, log level, commit, last events
	@.venv/bin/python -m tools.status

logs: _pi ## Follow the journal of the three units and the three app logs (Ctrl-C stops)
	@echo "journalctl -f -u airstation-collector -u airstation-manager -u airstation-dashboard"
	@echo "tail -F data/logs/*.log"
	@sh -c 'trap "kill 0" INT TERM EXIT; journalctl -f -n 10 -o short -u airstation-collector -u airstation-manager -u airstation-dashboard & tail -n 10 -F data/logs/*.log'

recovery: _pi ## Restore the database from last night's backup (asks; FORCE=1 skips)
	@[ -f $(DB).bak ] || { echo "No backup found ($(DB).bak)."; exit 1; }
	@[ "$(FORCE)" = "1" ] || { printf "Replace the current database with last night's backup? The current file is kept as airstation.db.broken. [y/N] "; \
		read a; [ "$$a" = "y" ] || { echo "Kept."; exit 1; }; }
	sudo systemctl stop $(UNITS)
	-mv $(DB) $(DB).broken
	rm -f $(DB)-wal $(DB)-shm
	cp $(DB).bak $(DB)
	sudo systemctl start $(UNITS)
	@systemctl is-active $(UNITS) || true
	@echo "Database restored from backup (at most one day old)."
	@echo "The previous file is $(DB).broken — delete it once things look right."

delete-data: _pi ## Delete ALL stored data — database, backup and logs (asks; FORCE=1 skips)
	@[ "$(FORCE)" = "1" ] || { printf "Delete ALL stored data (database + backup + logs)? [y/N] "; \
		read a; [ "$$a" = "y" ] || { echo "Kept."; exit 1; }; }
	sudo systemctl stop $(UNITS) 2>/dev/null || true
	rm -rf data
	mkdir -p data/logs
	sudo systemctl start $(UNITS) 2>/dev/null || true
	@echo "Data deleted; the apps start again with an empty database."

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
