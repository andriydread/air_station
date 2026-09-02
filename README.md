# Air Station

A home air-quality monitor on a Raspberry Pi Zero 2 W. It measures CO2,
temperature, humidity and particulate matter, shows them on an e-paper
panel, and serves a dashboard on the home network with live values,
history charts, diagnostics and controls. It is built to run unattended
for months: every part that can fail heals itself, and everything that
happens is written down.

This README explains what the project *is* and how it is put together —
for the owner coming back after six months, and for a coding assistant that
needs the architecture and the rules before touching anything. Installing
and operating it is the Makefile's job: `make help` on the Pi lists the
commands. Sensor details (calibration, warm-up, offsets, getting data off
the Pi) are in `docs/sensors.md`.

## Hardware

| Part | Bus | Role |
|---|---|---|
| Sensirion SCD41 | I2C | CO2 (ppm), altitude-compensated, photoacoustic |
| Sensirion SHT41 | I2C | Temperature, relative humidity |
| Sensirion SPS30 | I2C (0x69) | PM1 / PM2.5 / PM4 / PM10 + typical particle size |
| WeAct 3.7" e-paper, UC8253C | SPI0.0, GPIO RST=17 DC=25 BUSY=24 | 416×240 local display (portrait panel rotated 90°) |
| LX-2BUPS UPS + 1×18650 | — | Battery backup; no telemetry line, so power is watched via `vcgencmd` |

## How it is put together

Two programs run as two systemd services. They never talk to each other
directly; they share one SQLite file (`data/airmonitor.db`, WAL mode).

```
 airmonitor.service (main.py, "the collector")          airmonitor-web.service (dashboard/app.py)
 ┌──────────────────────────────────────────────┐        ┌─────────────────────────────────────┐
 │ main loop, 0.2 s tick over small timed jobs: │        │ Flask behind waitress on :8080       │
 │   sample sensors      every 10 s             │        │   reads state / measurements / events│
 │   run dashboard cmds  every 2 s              │        │   writes ONLY rows into `commands`   │
 │   publish status doc  every 30 s             │        │   renders the e-paper preview PNG    │
 │   power bits (vcgencmd) 60 s · disk 5 min    │        │ one HTML page + one JS file that     │
 │   e-paper frame       every 60 s (full 5 min)│        │ polls /api/summary every 10 s        │
 │   nightly: rollup → prune → backup           │        └──────────────────┬──────────────────┘
 │ threads: display render · weather 30 min ·   │                           │
 │          Wi-Fi probe 30 s (+ recovery ladder)│                           │
 │ heartbeat to systemd every 10 s              │                           │
 └───────────────────────┬──────────────────────┘                           │
                         ▼                                                   ▼
        data/airmonitor.db ── tables: measurements (10 s rows, 90 d) · measurements_hourly (forever)
                                      state (JSON docs by key) · commands (queue) · events (log, 14 d)
        data/logs/collector.log, dashboard.log
```

**The collector** (`main.py`, class `AirMonitor`) reads the sensors through
wrappers in `airmonitor/sensors.py`, runs the data-quality guards
(`airmonitor/quality.py`, `validation.py`), stores a row, and every minute
averages the last samples into an e-paper frame (`utils/display.py`
draws it, `lib/uc8253c.py` sends it). Slow things (a 15 s full e-paper
refresh, the weather fetch, the network probe) run on worker threads
(`airmonitor/workers.py`) so a sample is never late. Every 30 s it writes a
status document with the health of every subsystem into the `state` table.

**The dashboard** (`dashboard/`) only reads that database and shows four
tabs: **Live** (current values, forecast, e-paper preview), **History**
(charts, statistics, CSV / text export), **Diagnostics** (events, flagged
samples, connectivity, housekeeping), **Controls** (redraw display, fan
clean, CO2 calibration, service restart, reboot, delete history). A button
press becomes a row in `commands`; the collector executes it within 2 s and
writes the result back. The web process never touches hardware.

**Self-healing, layer by layer:** a sensor, the I2C bus or the display that
fails is re-created with exponential backoff; a sensor stuck returning
garbage or silence is re-initialised; six failed internet probes bounce the
Wi-Fi interface (Wi-Fi power save, the classic Zero 2 W hang, is disabled
at boot); the collector heartbeats systemd (`Type=notify`,
`WatchdogSec=90`) so a frozen process is restarted; the SoC hardware
watchdog reboots a frozen kernel; undervoltage/throttling is polled and
logged; the database gets a nightly integrity check, hourly rollups, pruning
and a rotating backup (`make recovery` restores it).

**Everything is logged twice:** every noteworthy event goes through
`EventLog` to the rotating log file *and* to the `events` table, which the
Diagnostics tab shows. Health changes, re-inits, flagged samples, command
results, Wi-Fi outages, power bits, starts (with the reason: reboot vs
restart, clean vs killed) and shutdowns all land there.

## Data model and its invariants

- `measurements`: one row per 10 s sample; eight metric columns plus a
  `flags` JSON column. A reading the quality guards reject (implausible
  jump, or taken during sensor warm-up) is stored in `flags` with its raw
  value and reason, and its metric column is NULL — so charts and averages
  stay clean while nothing is lost.
- `measurements_hourly`: one min/avg/max/count row per hour, folded nightly
  from raw rows; never pruned. Ranges beyond raw retention (90 days) and
  coarse buckets are served from here.
- `state`: JSON documents by key — `latest_measurements` (live values + per
  metric age), `collector_status` (every subsystem's health, uptime,
  calibration readiness), `latest_weather`, `latest_display_snapshot`,
  `scd41_last_calibration`, `collector_boot`. Unchanged documents are not
  rewritten (SD-card care).
- `commands`: `pending → running → succeeded | failed` with a JSON result.
- `events`: level, source, event_type, message, details; pruned at 14 days.
- **Schema changes are additive only** (`ALTER TABLE ADD COLUMN`,
  `CREATE IF NOT EXISTS`, see `_migrate_schema`): a database on the Pi must
  keep working after every `git pull`.
- Only the collector may fail leftover `running` commands at startup; the
  dashboard never touches commands the collector may be executing.
- Plausibility limits live in exactly one place: `airmonitor/validation.py`.

## Rules for anyone (or any assistant) editing this code

- **No hardware here, no hardware there.** Everything is verified with
  `make agent-test` (225 mocked tests, runs anywhere) and then on the Pi by
  the owner with `git pull && make deploy`. Running `main.py` off the Pi
  proves nothing; the `board`/`busio`/`RPi.GPIO`/`adafruit_*` imports are
  faked by `tests/conftest.py` for tests only.
- **Two audiences in the Makefile.** Plain targets run on the Pi as user
  `pi` (guarded by `_pi`); `agent-*` targets are for the development machine.
  Never add an unprefixed target meant for the dev machine.
- **Commands are fixed strings.** System actions (`systemctl restart …`,
  `reboot`) are spawned as literal strings with a 2 s delay; nothing from a
  dashboard payload is ever interpolated into a shell command. Destructive
  endpoints require an explicit server-side confirmation field.
- **Every hardware call goes through a health tracker and a backoff.** New
  device code follows `SensorHealth` + `ReinitBackoff` + `ensure()`; it
  never raises out of `read()`.
- **`dashboard.app` has no import side effects.** The app is built by
  `create_app()` (or `python -m dashboard.app`); never add a module-level
  `app`.
- **The drivers in `lib/` are not to be rewritten.** They are hand-written
  because no library exists for the UC8253C and the SPS30 pip options are
  UART-only; both are CRC/timing-checked against the datasheets and tested.
- **Frontend is vanilla JS + hand-rolled SVG, no dependencies.** Every
  `innerHTML` sink escapes. AQI and categories are computed in the backend
  only (`utils/aqi.py`).
- **Cadences that other code depends on:** sample 10 s, `measurement_max_age`
  45 s, status publish 30 s, display 60 s / full 300 s, systemd watchdog
  90 s. Change one, check the others.

## Configuration

Every setting has a default in `airmonitor/config.py` and an `AIRMONITOR_*`
environment override (set in `systemd/*.service`). The ones a person
actually changes:

| Variable | Default | Meaning |
|---|---|---|
| `AIRMONITOR_WEATHER_LAT` / `_LON` | Lviv | forecast location |
| `AIRMONITOR_SCD41_ALTITUDE_M` | 296 | sensor altitude for CO2 pressure compensation |
| `AIRMONITOR_SHT41_TEMP_OFFSET` | 0.0 | added to every temperature (negative if the Pi heats the sensor) |
| `AIRMONITOR_SCD41_ASC_ENABLED` | false | the CO2 sensor's automatic self-calibration (off: needs weekly fresh air) |
| `AIRMONITOR_KEEP_MEASUREMENTS_DAYS` | 90 | raw-row retention (hourly rollups are kept forever regardless) |
| `AIRMONITOR_DATABASE_PATH` | `data/airmonitor.db` | SQLite location (relative to the repo root) |

Everything else (intervals, thresholds, calibration limits, warm-up windows,
paths) is listed in `config.py` with a comment each. Paths are relative to
the working directory, so run things from the repo root.

## Where things live

```
main.py              the collector: setup, loop, timed jobs, status doc, shutdown
airmonitor/          collector package
  config.py            settings (+ env overrides, validation)
  sensors.py           SCD41 / SHT41 / SPS30 wrappers, health, auto-recovery, calibration safety
  quality.py           rate guard (spike flagging) + SHT41/SCD41 cross-check
  validation.py        plausibility limits — the single source
  storage.py           all SQL: schema, migrations, rollups, state, commands, events, backup
  commands.py          executes dashboard commands (incl. system actions via sudo)
  workers.py           display worker thread + periodic worker threads
  network.py           Wi-Fi / internet probe · wifi_recovery.py  escalating recovery ladder
  power.py             vcgencmd undervoltage/throttle bits · watchdog.py  sd_notify heartbeats
  lifecycle.py         explains each start (reboot vs restart, clean vs killed)
  logging_utils.py     rotating log + EventLog (log line + events row)
lib/                 hand-written drivers: sps30_i2c.py (I2C + CRC), uc8253c.py (e-paper SPI)
utils/               display.py (the e-paper picture, also the preview), weather.py (Open-Meteo), aqi.py (EPA math)
dashboard/           app.py (Flask routes) · templates/index.html · static/dashboard.js|.css
systemd/             the two service units, Wi-Fi power-save unit, sudoers grants, hardware-watchdog setup
assets/              e-paper font and weather icons
tests/               mocked test suite (conftest fakes the hardware modules)
docs/sensors.md      sensor care, calibration, warm-up, getting data out
datasheets/          Sensirion PDFs for SCD4x, SHT4x, SPS30
```

## When something looks wrong

1. **Diagnostics → Events.** Filter by the sensor's source. Health changes,
   re-inits, flagged samples, Wi-Fi outages and power bits are all there.
2. **History → Copy as text.** A paste-sized table for the range with the
   station's own events (`!` lines) and rejected samples (`~` lines)
   interleaved by time — the quickest way to correlate a spike with a
   restart or a Wi-Fi drop, and to hand the data to someone (or something)
   for a second opinion. `/api/export.csv` has every raw row.
3. **On the Pi:** `journalctl -u airmonitor -n 200`, and
   `data/logs/collector.log`.
4. **Controls** can redraw the display, restart either service or reboot;
   **`make recovery`** restores last night's database backup.

The dashboard has no authentication. It is meant for a trusted home LAN;
do not expose port 8080 to the internet.

## Developing off the Pi

```bash
make agent-venv          # local virtualenv with test dependencies (no hardware libs)
make agent-test          # the whole suite, ~9 s
python -m dashboard.app  # dashboard against a local/pulled data/airmonitor.db
```

Real data reaches a development machine only when the owner runs
`make push-data DEST=user@host` on the Pi. Visual checks of the dashboard
are done with a headless browser against a seeded database.
