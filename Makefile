# Air Monitor — deploy and service management.
#
# Two flows, pick one:
#   * ON THE PI (pull-based, recommended): log in, cd ~/air_station,
#     `git clone` once, then `make pi-deploy` after every push. See the
#     pi-* targets — they act on the local machine, no ssh.
#   * REMOTE (rsync-based): run deploy/deploy-full from a dev machine.
#     Override the target Pi like:  make deploy PI=pi@192.168.1.50
PI       ?= pi@pizero.local
APP_DIR  ?= ~/air_station
DATA_DIR ?= $(APP_DIR)/data
SSH       = ssh $(PI)
SERVICES  = airmonitor.service airmonitor-web.service

.PHONY: help deploy deploy-full install install-watchdog reinstall restart start stop status \
        test venv-dev logs logs-web pull-data venv clean uninstall wipe wipe-data nuke ssh db \
        pi-guard pi-deploy pi-fresh pi-restart pi-status pi-verify pi-watchdog

help: ## Show this help
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

venv: ## Create a local virtualenv and install dependencies
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

venv-dev: ## Local virtualenv with test dependencies only (no Pi hardware libs)
	python3 -m venv .venv
	.venv/bin/pip install -r requirements-dev.txt

test: ## Run the hardware-free test suite
	.venv/bin/python -m pytest tests/ -q

# --- On the Pi itself (pull-based flow) --------------------------------------
# Run these ON the Pi from ~/air_station. data/ and .venv are git-ignored,
# so no git operation here can touch the database or logs.

pi-guard:
	@[ "$$(whoami)" = "pi" ] && [ -d /etc/systemd/system ] || \
		{ echo "pi-* targets run ON the Pi, as user pi."; exit 1; }
	@git rev-parse --is-inside-work-tree >/dev/null

pi-deploy: pi-guard ## ON PI: reset to latest origin/main, deps, units, restart all
	git fetch origin
	git reset --hard origin/main
	git clean -fd   # drop stray/old files; ignored data/ and .venv survive (no -x!)
	[ -d .venv ] || python3 -m venv .venv
	.venv/bin/pip install -q -r requirements.txt
	mkdir -p data/logs
	sudo cp systemd/*.service /etc/systemd/system/
	sudo install -m 440 systemd/airmonitor-sudoers /etc/sudoers.d/airmonitor
	sudo visudo -c -q
	sudo systemctl daemon-reload
	sudo systemctl enable wifi-powersave-off.service $(SERVICES)
	sudo systemctl restart wifi-powersave-off.service $(SERVICES)
	@echo "Deployed from git."
	@$(MAKE) --no-print-directory pi-verify

pi-fresh: pi-guard ## ON PI: wipe venv + caches + stray files, then a clean pi-deploy (data/ kept)
	sudo systemctl stop $(SERVICES) 2>/dev/null || true
	rm -rf .venv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	@$(MAKE) --no-print-directory pi-deploy

pi-restart: pi-guard ## ON PI: restart both services
	sudo systemctl restart $(SERVICES)

pi-status: ## ON PI: show service status
	@systemctl --no-pager status $(SERVICES) || true

pi-verify: ## ON PI: post-deploy checklist (services, watchdog, power save, sudoers)
	@echo "--- services active:"; systemctl is-active $(SERVICES) || true
	@echo "--- systemd watchdog (want 1min 30s):"; systemctl show airmonitor -p WatchdogUSec || true
	@echo "--- wifi power save (want: off):"; iw dev wlan0 get power_save || true
	@echo "--- sudoers grant:"; sudo -n -l $$(command -v systemctl) restart airmonitor-web >/dev/null 2>&1 \
		&& echo "sudo -n OK" || echo "MISSING for $$(command -v systemctl) — check systemd/airmonitor-sudoers paths"
	@echo "--- last collector log lines:"; tail -n 15 data/logs/collector.log 2>/dev/null || true

pi-watchdog: pi-guard ## ON PI: arm the SoC hardware watchdog (then: sudo reboot)
	sh systemd/enable-watchdog.sh
	@echo "Hardware watchdog configured — run 'sudo reboot' to arm it."

# --- Remote (rsync) flow ------------------------------------------------------

deploy: ## Sync code to the Pi and restart both services
	rsync -avz --delete --filter="merge .rsync-filter" ./ $(PI):$(APP_DIR)
	$(SSH) "cd $(APP_DIR) && .venv/bin/pip install -q -r requirements.txt"
	$(SSH) "sudo systemctl restart $(SERVICES)"
	@echo "Deployed and restarted."

deploy-full: deploy ## Deploy + install updated systemd service files + sudoers
	$(SSH) "sudo cp $(APP_DIR)/systemd/*.service /etc/systemd/system/ \
		&& sudo install -m 440 $(APP_DIR)/systemd/airmonitor-sudoers /etc/sudoers.d/airmonitor \
		&& sudo visudo -c -q \
		&& sudo systemctl daemon-reload \
		&& sudo systemctl enable --now wifi-powersave-off.service \
		&& sudo systemctl restart $(SERVICES)"

install: ## First-time setup on the Pi (venv, deps, systemd units)
	rsync -avz --delete --filter="merge .rsync-filter" ./ $(PI):$(APP_DIR)
	$(SSH) "mkdir -p $(DATA_DIR)/logs"
	$(SSH) "cd $(APP_DIR) && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
	$(SSH) "sudo cp $(APP_DIR)/systemd/*.service /etc/systemd/system/ \
		&& sudo install -m 440 $(APP_DIR)/systemd/airmonitor-sudoers /etc/sudoers.d/airmonitor \
		&& sudo visudo -c -q \
		&& sudo systemctl daemon-reload \
		&& sudo systemctl enable --now wifi-powersave-off.service \
		&& sudo systemctl enable --now $(SERVICES)"

install-watchdog: ## Enable the hardware watchdog on the Pi (reboot required after)
	$(SSH) "sh $(APP_DIR)/systemd/enable-watchdog.sh"

reinstall: uninstall install ## Remove services, then run a clean install

restart: ## Restart collector + dashboard on the Pi
	$(SSH) "sudo systemctl restart $(SERVICES)"

start: ## Start collector + dashboard on the Pi
	$(SSH) "sudo systemctl start $(SERVICES)"

stop: ## Stop collector + dashboard on the Pi
	$(SSH) "sudo systemctl stop $(SERVICES)"

status: ## Show service status on the Pi
	$(SSH) "systemctl status $(SERVICES) --no-pager" || true

logs: ## Tail the collector log on the Pi
	$(SSH) "tail -n 100 -f $(DATA_DIR)/logs/collector.log"

logs-web: ## Tail the dashboard log on the Pi
	$(SSH) "tail -n 100 -f $(DATA_DIR)/logs/dashboard.log"

pull-data: ## Copy database + logs from the Pi into ./from_pi/data
	mkdir -p from_pi/data
	rsync -avz $(PI):$(DATA_DIR)/ from_pi/data/

ssh: ## Open an interactive shell on the Pi in the app directory
	$(SSH) -t "cd $(APP_DIR) && exec \$$SHELL -l"

db: ## Open a sqlite3 shell on the Pi's database
	$(SSH) -t "sqlite3 $(DATA_DIR)/airmonitor.db"

clean: ## Remove local virtualenv, caches and pulled data
	rm -rf .venv from_pi
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type f -name '*.pyc' -delete

uninstall: ## Stop + disable services and remove their .service files (keeps code + data)
	$(SSH) "sudo systemctl disable --now $(SERVICES) 2>/dev/null || true; \
		sudo rm -f /etc/systemd/system/airmonitor.service /etc/systemd/system/airmonitor-web.service; \
		sudo systemctl daemon-reload; sudo systemctl reset-failed 2>/dev/null || true"
	@echo "Services stopped, disabled and removed from the Pi."

wipe-data: ## Delete the database + logs on the Pi (FORCE=1 skips the prompt)
	@[ "$(FORCE)" = "1" ] || { printf "Delete ALL data in $(DATA_DIR) on $(PI)? [y/N] "; read a; [ "$$a" = "y" ] || exit 1; }
	$(SSH) "rm -rf $(DATA_DIR)"
	@echo "Database and logs removed from the Pi."

wipe: ## Delete the whole project directory on the Pi (FORCE=1 skips the prompt)
	@[ "$(FORCE)" = "1" ] || { printf "Delete the ENTIRE project dir $(APP_DIR) on $(PI)? [y/N] "; read a; [ "$$a" = "y" ] || exit 1; }
	$(SSH) "rm -rf $(APP_DIR)"
	@echo "Project directory removed from the Pi."

nuke: uninstall ## Full teardown: remove services AND the whole project dir on the Pi
	@$(MAKE) --no-print-directory wipe
	@echo "Pi fully wiped. Run 'make install' for a clean deployment."
