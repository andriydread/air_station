# Air Station

An indoor air quality monitor built on a Raspberry Pi Zero 2 W. It measures
CO2, particulates, temperature and humidity, shows them on an e-paper display
and serves a small web dashboard on the local network.

## Hardware

- Raspberry Pi Zero 2 W
- Sensirion SCD41 (CO2), SPS30 (PM1-PM10), SHT41 (temperature, humidity), all on I2C
- 3.7" e-paper display (UC8253C) on SPI

## How it works

Three Python services run under systemd and share one SQLite database (WAL
mode, so they can read and write at the same time):

- **collector** owns the I2C bus. Every 10 seconds it reads all three
  sensors and stores one row with the raw values. It also runs CO2
  calibration and SPS30 fan cleaning when asked.
- **manager** owns the display and the rest of the Pi. Every minute it
  averages the last six readings, calculates the AQI and redraws the panel.
  It also fetches the forecast from Open-Meteo, checks Wi-Fi, builds hourly
  averages and makes a nightly backup.
- **dashboard** is a Flask app with the web UI and a JSON API.

The services never talk to each other directly. Readings, status and
commands all go through the database, so any of them can restart without
taking the others down.

## E-paper display

- AQI from PM2.5 (EPA scale) and CO2 in ppm, each with a category word
- Indoor temperature and humidity
- Weather forecast in three 3-hour blocks: icon, high/low and chance of rain
- Partial refresh every minute, full refresh every 5 minutes to clear ghosting

## Web dashboard

- **Live** - current readings with sparklines and dew point
- **History** - charts for any time range, CSV export
- **Vitals** - Pi temperature, CPU, memory, Wi-Fi signal, throttling
- **Diagnostics** - event log, sensor errors and resets
- **Controls** - CO2 calibration, fan cleaning, service restarts, reboot
- **Data** - browse any database table as stored

## Reliability

The station is meant to run for weeks without anyone touching it:

- systemd watchdog on every service, plus the Pi's hardware watchdog
- a sensor that keeps returning bad values or goes silent gets re-initialised
- if the collector stops writing for 3 minutes, the manager restarts it
- Wi-Fi is reset after repeated failed checks
- raw data is kept for 30 days, hourly averages forever, with a nightly backup
- every service writes plain `key=value` logs, one file per day

## Repo layout

```
collector/   sensor reading
manager/     display, weather, maintenance
dashboard/   web UI and API
shared/      database, config, logging, scheduler
drivers/     SPS30 and e-paper drivers
systemd/     service files
docs/        sensor notes (calibration, resets)
tests/       tests with fake hardware (no Pi needed)
```
