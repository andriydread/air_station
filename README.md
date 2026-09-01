# Air Station

A self-contained air-quality monitor built on a Raspberry Pi Zero 2 W. It
measures CO2, temperature, humidity, and particulate matter, shows the
current state on a low-power e-paper display, and serves a web dashboard
with live values, history charts, diagnostics, and full remote control.

## Hardware

| Part | Bus | Role |
|---|---|---|
| Sensirion SCD41 | I2C | CO2 (ppm), altitude-compensated |
| Sensirion SHT41 | I2C | Temperature, relative humidity |
| Sensirion SPS30 | I2C (0x69) | Particulate matter: PM1 / PM2.5 / PM4 / PM10 + typical particle size |
| WeAct 3.7" e-paper, UC8253C | SPI | Local display, 416×240 (240×416 panel rotated 90°) |
| LX-2BUPS UPS + 1×18650 3.5Ah | — | Battery backup / brown-out protection |

Display wiring (BCM): RST=17, DC=25, BUSY=24, SPI0 CE0. Power rail is
buffered with 2×470µF electrolytic + 2×103 ceramic capacitors; the UPS
output carries another 470µF.

## How it works

Two systemd services share one SQLite database (WAL mode) at
`data/airmonitor.db`:

- **airmonitor** (`main.py`) — the collector. The main loop reads all
  sensors every 10s, runs data-quality guards, stores the sample, and
  executes dashboard commands. Slow subsystems live on worker threads so
  they can never stall sampling: the e-paper renderer (partial refresh
  every 60s, deep full refresh every 5 min), the Open-Meteo weather fetch
  (30 min), and the Wi-Fi probe (30s).
- **airmonitor-web** (`dashboard/`) — dashboard at `http://<pi>:8080`
  (served by waitress). Tabs: **Live** (metric cards with per-metric
  freshness badges, forecast, a pixel-exact preview of the e-paper
  screen), **History** (charts with presets or any custom date range,
  min/avg/max statistics, CSV export), **Diagnostics** (filterable event
  log, flagged samples, recent commands, connectivity + power detail),
  **Controls** (display refresh, SPS30 fan cleaning, SCD41 calibration,
  history deletion, service restarts and Pi reboot). Actions are queued
  as command rows in SQLite; the collector claims, executes, and stores
  the result — the web process never touches the hardware.

## Reliability

Built for unattended operation; every layer heals itself:

- **Sensor recovery** — a sensor (or the whole I2C bus, or the display)
  that fails at boot or mid-run is re-initialized automatically with
  exponential backoff. A sensor stuck returning garbage is restarted
  after a streak of bad readings.
- **Data trust** — plausibility limits live in one module
  (`airmonitor/validation.py`); physically impossible jumps are stored
  as *flagged* raw values (visible in Diagnostics) instead of polluting
  charts; the SCD41's own temperature/humidity are cross-checked against
  the SHT41 so a silently drifting sensor raises an event; the SCD41 is
  altitude-compensated (`AIRMONITOR_SCD41_ALTITUDE_M`).
- **Wi-Fi self-healing** — after consecutive failed connectivity probes
  the collector bounces the interface, then restarts the networking
  service (passwordless sudo for exactly those commands via
  `systemd/airmonitor-sudoers`). Wi-Fi power save is disabled by a
  oneshot unit — the classic Zero 2 W hang cause.
- **Watchdogs** — the collector heartbeats systemd (`Type=notify`,
  `WatchdogSec=90`): a wedged process is restarted, not just a crashed
  one. `make init` additionally arms the SoC hardware watchdog so a hard
  kernel freeze reboots the Pi within 15s.
- **Power visibility** — `vcgencmd get_throttled` is polled every minute;
  undervoltage/throttling flags become events and a status pill.
- **SD-card care** — unchanged state is never rewritten, and the command
  queue is polled without write transactions; the database lives through
  deploys (`data/` is git-ignored, so no deploy can touch it).
- **Database self-defense** — the nightly maintenance task runs a SQLite
  integrity check (corruption becomes an error event and an unhealthy
  storage state instead of silent data loss) and writes a rotating online
  backup next to the live file (`airmonitor.db.bak` + one previous
  generation), skipped automatically when disk headroom is tight. Free
  disk space is watched continuously and warned about below
  `AIRMONITOR_MIN_FREE_DISK_MB`. If the database ever does go bad, one
  command on the Pi restores it: `make recovery` (stops the services,
  swaps in the backup, keeps the broken file for inspection, restarts).

## Project layout

```
main.py            collector entry point: sampling loop + worker threads
airmonitor/        core package
  config.py          all settings (env-overridable, sensible defaults)
  sensors.py         SCD41 / SHT41 / SPS30 wrappers, health, auto-recovery
  quality.py         spike flagging + SHT41/SCD41 cross-check
  validation.py      the single source of plausibility limits
  commands.py        executes dashboard-queued commands (incl. system actions)
  workers.py         display worker + periodic workers
  network.py         Wi-Fi / internet probe (/sys, /proc, TCP check)
  wifi_recovery.py   escalating Wi-Fi recovery ladder
  power.py           vcgencmd undervoltage/throttle monitoring
  watchdog.py        sd_notify heartbeats for the systemd watchdog
  storage.py         SQLite: measurements(+flags), state, commands, events
  logging_utils.py   rotating-file logging + persisted event log
lib/               low-level drivers (SPS30 I2C with CRC, UC8253C SPI)
utils/             e-paper rendering, weather fetch, EPA AQI math,
                   standalone SCD41 recalibration script
assets/            icons and fonts for the e-paper UI
dashboard/         Flask app + vanilla JS/CSS frontend (waitress-served)
systemd/           service units, sudoers grants, watchdog provisioning
tests/             hardware-free test suite (mocked sensors; runs anywhere)
```

## Setup and daily use (Makefile)

All day-to-day commands run **on the Pi** from `~/air_station`
(one-time `git clone` first). The routine is: `git pull`, then
`make deploy`.

**First time / migrating from a pre-git install** (an `~/air_station`
that wasn't cloned — e.g. the old rsync era — can't `git pull`; replace
it, keeping the database):

```bash
sudo systemctl stop airmonitor airmonitor-web 2>/dev/null || true
mv ~/air_station/data ~/air_station_data          # keep the database
rm -rf ~/air_station
git clone https://github.com/andriydread/air_station.git ~/air_station
mv ~/air_station_data ~/air_station/data
cd ~/air_station && make init
sudo reboot                                       # arms the hardware watchdog
```

Skip the two `mv` lines to start with an empty database instead.

```bash
make init           # first time: fresh venv, requirements, services, watchdog
                    # (reboot once afterwards to arm the hardware watchdog)
make deploy         # after a git pull: requirements + new/updated service
                    # files, restart everything, quick health readout
make restart        # restart the app services
make delete-all     # remove services + venv + caches (asks before data)
make delete-venv    # delete the virtualenv
make delete-service # stop, disable and remove service files + sudoers
make delete-data    # delete ALL stored data — requires confirmation
make push-data DEST=user@host   # upload database + logs to the dev server
```

`deploy` never touches `data/` (the database) — only `delete-data` can,
and it asks first.

The `agent-*` targets (`make help` lists them) belong to the coding agent
on the dev server: tests, dev venv, cleanup. The dev server cannot reach
the Pi (home LAN), so real data travels the other way — `make push-data`
from the Pi when readings need tuning.

## Development off the Pi

The collector needs real hardware, but everything else runs anywhere —
the test suite fakes all of it (sensors, GPIO, SPI):

```bash
make agent-venv          # local virtualenv with test dependencies
make agent-test          # full suite: 130+ tests, no Pi needed
python -m dashboard.app  # dashboard against a local/pulled data/airmonitor.db
```

## Configuration

Every setting has a default in `airmonitor/config.py` and an
`AIRMONITOR_*` environment override (set them in the systemd units).
The ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `AIRMONITOR_SAMPLE_INTERVAL` | 10 | seconds between sensor reads |
| `AIRMONITOR_PARTIAL_UPDATE_INTERVAL` | 60 | e-paper partial refresh |
| `AIRMONITOR_FULL_UPDATE_INTERVAL` | 300 | e-paper full (anti-ghosting) refresh |
| `AIRMONITOR_WEATHER_LAT` / `_LON` | Lviv | forecast location |
| `AIRMONITOR_SCD41_ALTITUDE_M` | 296 | sensor altitude for CO2 compensation |
| `AIRMONITOR_SHT41_TEMP_OFFSET` | 0.0 | added to every temp reading (negative if self-heated) |
| `AIRMONITOR_MIN_VALID_CO2_PPM` | 350 | CO2 readings below this are glitches |
| `AIRMONITOR_SCD41_ASC_ENABLED` | false | SCD41 automatic self-calibration |
| `AIRMONITOR_SCD41_REINIT_AFTER_INVALID` | 30 | bad readings in a row before sensor auto-restart |
| `AIRMONITOR_SCD41_CALIBRATION_REMINDER_DAYS` | 180 | warn when the last forced calibration is older (0 = off) |
| `AIRMONITOR_WIFI_RECOVERY_AFTER_FAILURES` | 6 | failed probes per recovery action (0 = off) |
| `AIRMONITOR_KEEP_MEASUREMENTS_DAYS` | 90 | history retention (0 = forever) |
| `AIRMONITOR_KEEP_EVENTS_DAYS` | 14 | event-log retention |
| `AIRMONITOR_MIN_FREE_DISK_MB` | 200 | low-disk warning threshold (0 = off) |
| `AIRMONITOR_ALLOWED_HOSTS` | *(empty)* | dashboard answers only these hostnames/IPs, comma-separated (DNS-rebinding guard; empty = any) |
| `AIRMONITOR_DATABASE_PATH` | `data/airmonitor.db` | SQLite location |

## Sensor care

What the station does (and deliberately doesn't do) to keep the sensors
truthful and healthy:

- **Burn-in: not required.** None of these sensors are metal-oxide (MOX)
  types; NDIR (SCD41), capacitive (SHT41), and optical (SPS30) elements
  need no conditioning period. The SCD41's first sample arrives ~5s after
  measurement start; the collector waits for the sensor's own data-ready
  signal, and start-up glitches (e.g. 0 ppm) are filtered by
  `AIRMONITOR_MIN_VALID_CO2_PPM` and the rate guard.
- **SCD41 calibration — the important one.** ASC (automatic
  self-calibration) is **off by default** on purpose: ASC assumes the
  sensor sees fresh ~400 ppm air at least weekly, and in a continuously
  occupied room it slowly drags the baseline wrong. The trade-off: with
  ASC off, NDIR drift is corrected only by **forced recalibration (FRC)**
  — do one after installation and then a few times a year, in fresh
  outdoor air (target 420 ppm), via the Controls tab. The collector
  refuses an unsafe FRC (minimum runtime, sample count, reading
  stability, distance from target all enforced) and reminds you with a
  `calibration_due` event when the last FRC is older than
  `AIRMONITOR_SCD41_CALIBRATION_REMINDER_DAYS`. If the station ever moves
  somewhere regularly ventilated, flipping ASC on from Controls is valid
  — the reminder then silences itself.
- **SCD41 environment compensation**: altitude is written to the sensor
  before every measurement start (`AIRMONITOR_SCD41_ALTITUDE_M`) — CO2
  math is measurably wrong without it.
- **SHT41**: factory-calibrated, no user calibration exists. The real
  enemy is self-heating from the Pi; measure against a reference
  thermometer and set `AIRMONITOR_SHT41_TEMP_OFFSET` (negative) to
  correct the mounting. Its readings are cross-checked against the
  SCD41's internal sensors — sustained disagreement raises a
  `sensor_disagreement` event, catching silent drift no single sensor
  can self-report.
- **SPS30 fan hygiene**: the sensor's built-in automatic fan cleaning
  runs weekly (its power-on default; adjustable from Controls). A manual
  clean is available too — rate-limited to once per 30 min, and readings
  are blanked for 15s while the fan runs at full speed so cleaning junk
  never enters the history. Sensirion's stated lifetime assumes the
  weekly cleaning stays enabled; don't set the interval to 0 without a
  reason.
- **Safe shutdown**: on service stop the SCD41's periodic measurement is
  stopped and the SPS30 is stopped *and put to sleep* (fan off) — power
  cycles and reboots never catch the fan spinning or leave a sensor
  mid-command.

## Maintenance notes

- **SCD41 recalibration**: from the dashboard Controls tab (preconditions
  enforced, see Sensor care), or interactively with
  `python utils/recalibrate_SCD41.py` on the Pi in fresh outdoor air.
- **When data looks wrong**: check Diagnostics first — the event log
  (sensor state changes, invalid readings, network drops, power flags,
  command results) and the flagged-samples panel say what the station
  itself thinks happened.
- **After changing systemd files or sudoers**: nothing special —
  `make deploy` always refreshes units + sudoers along with the code, so
  the `Type=notify` watchdog unit and its matching collector land together.
