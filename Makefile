# Air Station
#
# On the Pi:   make init (first time, then reboot), git pull && make deploy
# Dev machine: make dev, make test, make demo

UNITS  = airstation-collector airstation-manager airstation-dashboard
ME    := $(shell whoami)
RENDER = sed -e 's|@USER@|$(ME)|g' -e 's|@REPO@|$(CURDIR)|g'
DB     = data/airstation.db

.PHONY: help init deploy restart status logs export recovery delete-data \
        dev test demo import clean _pi _apt _install

help: ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

# --- on the Pi ---

_pi:  # the Pi targets need systemd and I2C, and must not run as root
	@[ -d /etc/systemd/system ] && [ -e /dev/i2c-1 ] || { echo "This target runs ON the Pi."; exit 1; }
	@[ "$$(id -u)" != 0 ] || { echo "Run make without sudo, it calls sudo itself."; exit 1; }

_install: _pi  # venv packages, systemd units, sudoers, journald drop-in
	[ -x .venv/bin/python ] || python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	mkdir -p data/logs
	for unit in $(UNITS); do \
		$(RENDER) systemd/$$unit.service.in | sudo tee /etc/systemd/system/$$unit.service >/dev/null; \
	done
	sudo cp systemd/wifi-powersave-off.service /etc/systemd/system/
	$(RENDER) systemd/airstation-sudoers.in > /tmp/airstation-sudoers
	sudo visudo -c -q -f /tmp/airstation-sudoers
	sudo install -m 440 /tmp/airstation-sudoers /etc/sudoers.d/airstation
	rm -f /tmp/airstation-sudoers
	sudo install -d -m 2755 -g systemd-journal /var/log/journal
	sudo install -D -m 644 systemd/journald-airstation.conf /etc/systemd/journald.conf.d/airstation.conf
	sudo systemctl restart systemd-journald
	sudo systemctl daemon-reload

_apt: _pi
	@[ -e /dev/spidev0.0 ] || { echo "Enable SPI in raspi-config and reboot first."; exit 1; }
	sudo apt-get install -y python3-venv python3-dev swig liblgpio-dev

init: _apt _install ## First install (enable I2C and SPI in raspi-config first)
	sh systemd/enable-watchdog.sh
	sudo systemctl enable --now wifi-powersave-off $(UNITS)
	@echo "Done. Reboot once to arm the hardware watchdog."

deploy: _install ## Update after git pull
	sudo systemctl restart $(UNITS)
	@systemctl is-active $(UNITS) || true

restart: _pi ## Restart the three services
	sudo systemctl restart $(UNITS)

status: _pi ## Services, data ages, database, disk
	@.venv/bin/python -m tools.status

logs: _pi ## Follow the service journal
	journalctl -f -u airstation-collector -u airstation-manager -u airstation-dashboard

export: _pi ## Pack database, logs and journal into ~/airstation-<stamp>.tar.gz
	@sh tools/export.sh

recovery: _pi ## Restore the database from last night's backup
	@[ -f $(DB).bak ] || { echo "No backup at $(DB).bak"; exit 1; }
	@printf "Replace the database with last night's backup? [y/N] "; read a; [ "$$a" = y ]
	sudo systemctl stop $(UNITS)
	-mv $(DB) $(DB).broken
	rm -f $(DB)-wal $(DB)-shm
	cp $(DB).bak $(DB)
	sudo systemctl start $(UNITS)

delete-data: _pi ## Delete the database, backup and logs
	@printf "Delete ALL stored data? [y/N] "; read a; [ "$$a" = y ]
	sudo systemctl stop $(UNITS)
	rm -rf data && mkdir -p data/logs
	sudo systemctl start $(UNITS)

# --- dev machine (no hardware needed) ---

dev: ## Create .venv with test dependencies
	python3 -m venv .venv
	.venv/bin/pip install -q -r requirements-dev.txt

test: ## Run the tests
	.venv/bin/python -m pytest tests/ -q

demo: ## Run all three apps with fake sensors on :8080
	.venv/bin/python tools/demo.py --reset --seed-hours 48

import: ## Unpack an export archive into from_pi/ (FILE=...)
	@[ -n "$(FILE)" ] || { echo "Usage: make import FILE=airstation-<stamp>.tar.gz"; exit 1; }
	.venv/bin/python -m tools.import_archive $(FILE)

clean: ## Remove .venv, caches and imported data
	rm -rf .venv from_pi .pytest_cache
	find . -name __pycache__ -prune -exec rm -rf {} +
