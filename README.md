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
  one. `make install-watchdog` additionally arms the SoC hardware
  watchdog so a hard kernel freeze reboots the Pi within 15s.
- **Power visibility** — `vcgencmd get_throttled` is polled every minute;
  undervoltage/throttling flags become events and a status pill.
- **SD-card care** — unchanged state is never rewritten, and the command
  queue is polled without write transactions; the database lives through
  deploys (`.rsync-filter` protects `data/`).

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

The Pi is reachable as `pi@pizero.local` by default; override with
`make <target> PI=pi@<addr>`.

```bash
make install          # first-time: code, venv, systemd units, sudoers
make install-watchdog # arm the hardware watchdog (reboot Pi after)
make deploy           # sync code, install deps, restart both services
make deploy-full      # deploy + refresh systemd units + sudoers
make restart / stop / start / status
make logs / logs-web  # tail collector / dashboard log on the Pi
make pull-data        # copy database + logs from the Pi to ./from_pi/data
make db               # sqlite3 shell on the Pi's live database
```

## Development off the Pi

The collector needs real hardware, but everything else runs anywhere —
the test suite fakes all of it (sensors, GPIO, SPI):

```bash
make venv-dev            # local virtualenv with test dependencies
make test                # full suite: 125+ tests, no Pi needed
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
| `AIRMONITOR_WIFI_RECOVERY_AFTER_FAILURES` | 6 | failed probes per recovery action (0 = off) |
| `AIRMONITOR_KEEP_MEASUREMENTS_DAYS` | 90 | history retention (0 = forever) |
| `AIRMONITOR_KEEP_EVENTS_DAYS` | 14 | event-log retention |
| `AIRMONITOR_DATABASE_PATH` | `data/airmonitor.db` | SQLite location |

## Maintenance notes

- **SCD41 recalibration**: from the dashboard Controls tab (the sensor
  must be warmed up and in stable reference air — the collector enforces
  runtime, sample-count, and stability preconditions), or interactively
  with `python utils/recalibrate_SCD41.py` on the Pi in fresh outdoor air.
- **SPS30 fan cleaning**: automatic weekly by sensor default; can be
  forced from the dashboard (rate-limited to once per 30 min).
- **When data looks wrong**: check Diagnostics first — the event log
  (sensor state changes, invalid readings, network drops, power flags,
  command results) and the flagged-samples panel say what the station
  itself thinks happened.
- **After changing systemd files or sudoers**: `make deploy-full`, not
  plain `make deploy`. The `Type=notify` watchdog unit requires the
  matching collector code — always deploy both together.
