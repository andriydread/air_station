# Air Station

Indoor air-quality station on a Raspberry Pi Zero 2 W: a Sensirion SCD41
(CO2), SHT41 (temperature, humidity) and SPS30 (particulates), a 3.7" e-paper
panel on the wall, and a web dashboard for the phone. Three small programs
share one SQLite file; everything they see is written down.

## Hardware

| Part | Bus | Role |
|---|---|---|
| Sensirion SCD41 | I2C | CO2 (ppm), photoacoustic; also its own temperature and humidity |
| Sensirion SHT41 | I2C | Temperature, relative humidity |
| Sensirion SPS30 | I2C (0x69) | PM1 / PM2.5 / PM10 mass, particle counts 0.5 / 1 / 2.5 µm, typical particle size |
| WeAct 3.7" e-paper, UC8253C | SPI0.0, GPIO RST=17 DC=25 BUSY=24 | 416×240 1-bit panel, portrait module rotated 90° |
| LX-2BUPS UPS + 1×18650 | — | Battery backup; no telemetry line, so power is watched through `vcgencmd` |

## The three programs

Each is one single-threaded loop with its own systemd unit (`airstation-<name>`)
and a 90 s watchdog. They never talk to each other directly, only through the
tables in `data/airstation.db`.

**collector** owns the I2C bus.

- Every 30 s, five seconds after the :00/:30 mark (clear of the panel
  refresh), it reads the sensors one after another so no two draw current at
  once: the SHT41 measures, the SPS30 hands over its latest numbers, then the
  SCD41 is told to measure once (a 5 s single shot) and read. It drops garbage (corrupt
  words, negatives, values outside the sensor's range, CO2 below 350 ppm)
  and writes one raw row: 12 metrics, an empty cell where a value was dropped.
- After any start a sensor warms up first: 60 s for CO2, 30 s for dust. Cells
  stay empty, one `warming_up` event is logged, nothing counts against the sensor.
- Six bad readings in a row, or two minutes without any reading, re-initialise
  that sensor with a growing delay (30 s → 5 min). The bus itself is re-opened
  only when all sensors fail together.
- It also runs the two sensor commands from the dashboard (forced CO2
  calibration with safety checks, manual fan clean), the weekly fan clean on
  Sunday 04:00 local time, sends the live air pressure from the weather into
  the SCD41 every 30 min, and publishes its status document every 30 s.
- At start it waits up to 60 s for NTP, then writes anyway; clock jumps are logged.

**manager** owns the SPI bus and the machine. It is the only program with sudo.

- Every minute it averages the last 60 s of raw rows into `display_data`:
  AQI from PM2.5 only (EPA 2024 breakpoints, six categories; the panel says
  "Sensitive" for the long one), CO2 on the UBA scale (Good below 1000 ppm,
  Elevated below 2000, Poor above), the weather columns, the warning
  glyphs, the warming-up and collector-silent flags. Then it paints
  the panel: a partial refresh every minute, a full one on every 5-minute mark.
- Weather from Open-Meteo every 30 min (and once at start), cut into rolling
  3-hour blocks on the local clock; a forecast older than 6 h is painted as "—".
- Router and internet probes every 30 s; six router failures in a row bounce
  the Wi-Fi radio (`nmcli radio wifi off/on`, at most once per 10 min).
- A vitals row every minute (CPU temperature, load, memory, disk, database
  size, Wi-Fi signal, probe latencies, the throttled bits, uptime, collector lag).
- Hourly rollups at :00 (catch-up at start), the nightly job at 00:05 local
  (prune → checkpoint → backup to `data/airstation.db.bak`), commands nobody
  picked up failed after 10 min, the collector restarted after 5 min of silence.
- The system commands from the dashboard: restart collector, restart
  dashboard, reboot, delete history.

**dashboard** is Flask behind waitress on port 8080. Five tabs — Live,
History, Vitals, Diagnostics, Controls — read the tables and poll
`/api/changes` every 10 s (every second for 15 s after a button press). An
"RD" chip marks a value the redesign has no source for yet. The footer shows
the running commit and the three uptimes.

### What one beat looks like

```
:05  collector: SHT41 → SPS30 → SCD41 single shot (5 s) → one raw row stamped :00
:35  … and again, stamped :30
:00 of each minute   manager averages the two rows of the minute that ended one beat ago → display_data → panel
:00 of each hour     manager rolls the hour up into hourly_measurements
00:05 local          manager prunes, checkpoints, backs up
Sunday 04:00 local   collector runs the SPS30 fan clean
```

## The database

One file, `data/airstation.db`, WAL mode. Any program creates the schema on
open; there are no migrations. Timestamps are Unix seconds everywhere; local
time appears only where a person looks (panel clock, browser, the two local
schedules above).

| Table | One row per | Kept |
|---|---|---|
| `raw_measurements` | 10 s beat: `co2 co2_temp co2_humid temp humid pm1 pm25 pm10 tps nc05 nc1 nc25` | 90 days |
| `hourly_measurements` | hour: `samples` + min / max / avg of every metric | forever |
| `vitals` | minute of machine health | 30 days |
| `events` | thing worth remembering: `app level source type message details` | 30 days |
| `commands` | button press: `from_whom to_whom type status payload result` (pending → running → success / fail) | 30 days |
| `state` | key: small JSON documents, newest only | — |

The state documents: `display_data` (what the panel shows, rewritten every
minute — its age is the Live tab's freshness), `collector_status`,
`manager_status`, `last_weather`, `last_calibration`.

Event types are a fixed vocabulary in `shared/events.py`, grouped by source:
`scd41 / sht41 / sps30 / i2c` (`sensor_init`, `sensor_reinit`, `sensor_error`,
`value_dropped`, `warming_up`, `fan_clean`, `calibration_done`, …), `display`,
`weather`, `wifi`, `power`, `watch`, `storage`, `machine`, `app`, `web`. The
Diagnostics tab filters on them.

## Configuration — `config.toml`

Read once by all three programs at start; after editing run `make restart`.
Everything not in this file is a constant next to the code that uses it.

| Key | Meaning |
|---|---|
| `location.latitude`, `location.longitude` | Coordinates for the weather forecast, nothing else |
| `location.altitude_m` | Given to the SCD41 while no weather pressure is known yet |
| `sensors.scd41_temp_offset_c` | The SCD41's internal temperature offset (factory 4.0); tune it from the bench data |
| `sensors.sht41_temp_offset_c` | Subtracted from the SHT41 reading to correct the mounting |
| `sensors.asc` | SCD41 automatic self-calibration; off, see `docs/sensors.md` |
| `sensors.calibration_target_ppm` | Target for the forced calibration button (fresh air, 420) |
| `retention_days.raw` | Days of 30-second rows (90); hourly rows are kept forever |
| `retention_days.vitals`, `retention_days.events`, `retention_days.commands` | Days of machine health, events, button presses (30) |
| `retention_days.logs` | Daily log files kept per program (45, so a 30-day bench exports whole) |
| `weather.block_hours` | Width of the three forecast columns on the panel (3) |
| `dashboard.port` | The web port (8080) |
| `paths.database`, `paths.logs` | Where the data lives; relative paths resolve against this file's directory |
| `logging.level` | `debug` for the bench period, `info` afterwards |

## A fresh Pi, start to finish

1. **Raspberry Pi Imager:** Raspberry Pi OS Lite (64-bit). In the settings set
   the hostname, the user `pi` with a password, your Wi-Fi and locale, and
   enable SSH. Boot, log in over SSH.
2. **Enable the buses:** `sudo raspi-config` → Interface Options → enable
   I2C and SPI. `sudo reboot`. Afterwards `/dev/i2c-1` and `/dev/spidev0.0` exist.
3. **Get the code:**
   ```
   sudo apt-get install -y git
   git clone <this repository> ~/air_station
   cd ~/air_station
   ```
   Clone as your own user, never with `sudo`: the units run as the user who
   runs `make init`, and a checkout owned by root breaks the later `git pull`.
   If that already happened: `sudo chown -R $USER: <the checkout>`.
4. **Install:** `make init` (no `sudo` in front; it asks for it where needed).
   It checks the two device files, installs the apt
   packages, creates `.venv`, installs the requirements (the first install on
   a fresh card may compile for a while — that is expected), creates
   `data/logs`, renders the three unit files and the sudoers file with your
   user and this path, makes the system journal persistent (capped at
   200 MB, so an export can show the previous boot), enables the hardware
   watchdog, and enables and starts the four units (the three programs plus
   the Wi-Fi power-save switch).
5. `sudo reboot` once, to arm the hardware watchdog.

### The first hour

After the reboot, from the checkout:

- `make status` — all three units `active (running)`; the last raw row a few
  seconds old; `display_data` and `vitals` under a minute old.
- The panel shows "Warming up…" for the first minute, then real numbers.
- The dashboard answers on the phone at `http://<hostname>.local:8080`;
  Diagnostics shows three healthy sensors and a fresh weather fetch.
- `make export` writes an archive to your home directory.
- At the next full hour an `hourly` rollup appears; the morning after, a
  `nightly` event with the backup size.

Then, once, on a calm day: take the station outside or to an open window for
ten minutes and press **Calibrate CO2** on the Controls tab (see
`docs/sensors.md` for why).

## The `make` targets

On the Pi (they refuse to run anywhere else):

| Target | Does |
|---|---|
| `make init` | Fresh install, as above |
| `make deploy` | After `git pull`: requirements, unit files, sudoers; restarts the three programs |
| `make restart` | Restarts the three programs (after editing `config.toml`) |
| `make status` | One screen: units, data ages, database and backup, disk, log level, commit, last events |
| `make logs` | Follows the journal of the three units and the three log files at once |
| `make export` | Packs everything for analysis into `~/airstation-<stamp>.tar.gz` |
| `make recovery` | Restores last night's backup (asks; `FORCE=1` skips the question) |
| `make delete-data` | Deletes database, backup and logs and starts fresh (asks; `FORCE=1`) |
| `make help` | The list |

## The bench: logging as a test instrument

For the first weeks `logging.level = "debug"`: every raw value, every
timing, every error with its traceback goes into `data/logs/<program>.log`
as `key=value` lines, one file per UTC day. A line looks like

```
2026-09-03T12:00:10Z DEBUG collector scd41 sample co2=812 co2_temp=25.1 co2_humid=38.4 ms=412
```

`make export` builds `~/airstation-<YYYYMMDD-HHMM>.tar.gz` holding a
consistent copy of the database (`db/airstation.db`), every log file
(`logs/`), the journal of the three units for the last 30 days and the
kernel log of this and the previous boot (`journal/`), `vcgencmd`, `df`,
`free`, `uname` (`system/`), `config.toml` and `commit.txt`. Move it by hand
(Pi → laptop → wherever the analysis happens) and unpack it with
`make agent-import FILE=<archive>`, which prints the row counts. Nothing
about the analysis lands in this repository.

## Where things live

```
collector/   sensors, filters, sampling, the collector's commands, entry point
manager/     weather, display, machine, network, frame, maintenance, commands, entry point
dashboard/   Flask app, API, templates/, static/
shared/      config, db (schema + every query), events (logger + vocabulary),
             heartbeat, clock, loop (the scheduler), aqi, render (the panel picture), backoff
drivers/     SPS30 over I2C with CRC, UC8253C over SPI — hand-written, stable
tools/       status screen, export, backup, import, demo
systemd/     the three unit templates, sudoers template, Wi-Fi power-save unit, journald drop-in, watchdog setup
assets/      panel font, weather icons (moon.png is an empty slot: night blocks use sun.png until you add one)
docs/        sensors.md — how the sensors are cared for; workflow.md — each program step by step
datasheets/  Sensirion documents the filters and warm-ups follow
tests/       hardware-free suite; tests/mocks/ are the fake sensors, panel, clock
data/        database, backup, logs (git-ignored)
```

## When something looks wrong

- `make status` first: a unit not `active`, a raw row older than a minute,
  `display_data` older than two — each points at one program.
- Diagnostics tab: sensor health, bad-read streaks, re-init counts, the
  panel's last full and partial refresh, the last weather fetch, restart
  counts, the event list with filters.
- `make logs` for the live stream; the export for anything older.
- The events you will see most: `warming_up` and `sensor_init` after every
  start; `value_dropped` (first of a streak, then every sixth) when a sensor
  hands over garbage; `sensor_reinit` when it does so six times in a row;
  `internet_down` / `internet_up` on the home connection; `wifi_bounce` when
  the router itself vanished; `power_issue` when the Pi reports under-voltage;
  `collector_silent` / `collector_restarted` when the manager had to step in.
- A broken database: `make recovery` puts back last night's backup and keeps
  the broken file next to it.

## Developing off the Pi

The tests need no hardware: `tests/conftest.py` injects fake `board`,
`busio`, `RPi.GPIO`, `spidev` and the Adafruit drivers.

```
make agent-venv     # a virtualenv with the test dependencies only
make agent-test     # the whole suite
make agent-demo     # collector + manager + dashboard here on fake hardware, 48 h of seeded history, dashboard on :8080
make agent-demo-stop  # stop a demo left running in the background
make agent-import FILE=~/airstation-<stamp>.tar.gz   # unpack a bench archive into from_pi/
make agent-clean    # remove the venv, caches and from_pi/
```

Rules for anyone (or any assistant) editing this code:

- Simplicity, reliability, logging — in that order. Richness comes from
  logging more fields, never from more mechanisms: no worker threads, no
  environment-variable overrides, constants next to the code they tune.
- The three programs share the database and nothing else. A new value flows
  table → `display_data` or a status document → tab; never around them.
- Event types are the fixed list in `shared/events.py`; the tests reject an
  unknown one.
- Drivers in `drivers/` are not rewritten; their tests are the contract.
- Every change: `make agent-test` green, one commit per task, plain message.

The manager runs four commands as root through `/etc/sudoers.d/airstation`
without a password: the Wi-Fi radio off and on, restarting the collector and
the dashboard, and reboot. Nothing else; the collector and the dashboard have
no sudo at all.
