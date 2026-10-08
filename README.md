# Air Station

An indoor air quality monitor built on a Raspberry Pi Zero 2 W. It measures
CO2, particulates, temperature and humidity, shows them on an e-paper display
and serves a small web dashboard on the local network.

## Hardware

- Raspberry Pi Zero 2 W
- Sensirion SCD41 (CO2), SPS30 (PM1-PM10), SHT41 (temperature, humidity), all on I2C
- 3.7" e-paper display (UC8253C) on SPI

## How it works

Three Python services run under systemd and share one SQLite database:

- **collector** reads the sensors every 10 seconds and stores the raw values
- **manager** averages each minute, updates the display, fetches the weather
  forecast from Open-Meteo, and looks after the Pi (Wi-Fi, backups, restarts)
- **dashboard** is a Flask app with live readings, history charts, system
  health, CSV export and a few controls (CO2 calibration, fan cleaning)

Each service has a systemd watchdog, and sensors that start returning bad
readings are reset automatically.

## Repo layout

```
collector/   sensor reading
manager/     display, weather, maintenance
dashboard/   web UI and API
shared/      database, config, logging, scheduler
drivers/     SPS30 and e-paper drivers
systemd/     service files
tests/       tests with fake hardware (no Pi needed)
```

`make test` runs the tests and `make demo` runs the whole station locally
with simulated sensors.
