# Air Station

A self-contained air-quality monitor built on a Raspberry Pi Zero 2 W. It
measures CO2, temperature, humidity, and particulate matter, shows the
current state on a low-power e-paper display, and serves a web dashboard
with live values, history charts, diagnostics, and sensor controls.

## Hardware

| Part | Bus | Role |
|---|---|---|
| Sensirion SCD41 | I2C | CO2 (ppm) |
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

- **airmonitor** (`main.py`) — the collector. A loop of small periodic
  tasks: read all sensors every 10s and store the sample; refresh the
  e-paper every 60s (deep full refresh every 5 min to clear ghosting);
  fetch an Open-Meteo forecast every 30 min; probe Wi-Fi/internet every
  30s; prune old rows daily. Sensor failures never crash the loop — every
  sensor has a health record, invalid readings are dropped and logged, and
  a stuck SCD41 is automatically re-initialized (a real failure mode:
  the sensor can silently start returning 0 ppm until restarted).
- **airmonitor-web** (`dashboard/`) — Flask dashboard at
  `http://<pi>:8080`. Live metric cards with AQI, history charts
  (6h/24h/3d/7d), forecast, the persisted event log, and controls: SPS30
  fan cleaning, SCD41 forced calibration (guarded by warm-up/stability
  preconditions) and ASC toggle, history deletion. Actions are queued as
  command rows in SQLite; the collector claims, executes, and stores the
  result — so the web process never touches the hardware.

## Project layout

```
main.py            collector entry point: the periodic-task loop
airmonitor/        core package
  config.py          all settings (env-overridable, sensible defaults)
  sensors.py         SCD41 / SHT41 / SPS30 wrappers + health tracking
  commands.py        executes dashboard-queued commands
  network.py         Wi-Fi / internet probe (/sys, /proc, TCP check)
  storage.py         SQLite: measurements, state, command queue, events
  logging_utils.py   rotating-file logging + persisted event log
lib/               low-level drivers (SPS30 I2C with CRC, UC8253C SPI)
utils/             e-paper rendering, weather fetch, EPA AQI math,
                   standalone SCD41 recalibration script
assets/            icons and fonts for the e-paper UI
dashboard/         Flask app + vanilla JS/CSS frontend
systemd/           service units for both processes
tests/             hardware-free test suite (mocked sensors; run anywhere)
```

## Setup and daily use (Makefile)

The Pi is reachable as `pi@pizero.local` by default; override with
`make <target> PI=pi@<addr>`.

```bash
make install     # first-time: rsync code, create venv, install systemd units
make deploy      # sync code, install deps, restart both services
make deploy-full # deploy + refresh systemd unit files
make restart / stop / start / status
make logs        # tail the collector log on the Pi
make logs-web    # tail the dashboard log on the Pi
make pull-data   # copy database + logs from the Pi to ./from_pi/data
make db          # sqlite3 shell on the Pi's live database
```

`data/` (database, logs) is protected by `.rsync-filter` and survives
deploys and `--delete` syncs.

## Development off the Pi

The collector needs real hardware, but everything else runs anywhere:

```bash
make venv                # local virtualenv + runtime deps
python -m dashboard.app  # dashboard against a local/pulled data/airmonitor.db
make test                # full test suite with mocked hardware (no Pi needed)
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
| `AIRMONITOR_MIN_VALID_CO2_PPM` | 350 | CO2 readings below this are glitches |
| `AIRMONITOR_SCD41_ASC_ENABLED` | false | SCD41 automatic self-calibration |
| `AIRMONITOR_SCD41_REINIT_AFTER_INVALID` | 30 | bad readings in a row before auto-restart of the sensor |
| `AIRMONITOR_KEEP_MEASUREMENTS_DAYS` | 90 | history retention (0 = forever) |
| `AIRMONITOR_KEEP_EVENTS_DAYS` | 14 | event-log retention |
| `AIRMONITOR_DATABASE_PATH` | `data/airmonitor.db` | SQLite location |

## Maintenance notes

- **SCD41 recalibration**: from the dashboard (sensor must be warmed up and
  in stable reference air — the collector enforces runtime, sample-count,
  and stability preconditions before it will run), or interactively with
  `python utils/recalibrate_SCD41.py` on the Pi in fresh outdoor air.
- **SCD41 stuck at 0 ppm**: auto-recovered since the July 2026 incident —
  after 30 consecutive invalid readings (~5 min) the collector restarts the
  sensor and logs the event.
- **SPS30 fan cleaning**: automatic weekly by sensor default; can be forced
  from the dashboard (rate-limited to once per 30 min).
- **Diagnostics**: everything notable (sensor state changes, invalid
  readings, network drops, command results) is persisted to the `events`
  table and visible in the dashboard's Recent Events panel — check there
  first when data looks wrong.
