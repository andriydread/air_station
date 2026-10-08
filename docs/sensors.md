# Sensor notes

How the collector handles the three Sensirion sensors. Constants live next to
the code in `collector/sensors.py` and `shared/filters.py`; the four settings
you can change are under `[sensors]` in `config.toml`.

## Start-up

None of these sensors need a burn-in, but they do need a moment after each
start (the SPS30 fan and laser take 8-30 s, the SCD41 needs to settle
thermally). So after any start or re-init a sensor is left alone until the
first full minute at least 60 s away (`ready_at`). The panel shows
"Starting up" in the meantime.

On shutdown the collector stops the SCD41 and SPS30 measurements, so every
start is a cold start for both.

## Bad readings and resets

Every value is stored as the sensor gave it. Values that can't be real air
(NaN, negative counts, temperature outside -40..85 °C, humidity outside
0..100 %, CO2 below 10 or above 40000 ppm) are still stored, but they count
as bad readings and are left out of the panel's minute average.

A sensor gets re-initialised after 6 bad readings in a row, 6 within three
minutes, or a full minute with no answer. A failed init is retried with
backoff from 30 s up to 5 min. If all sensors fail in the same read, the I2C
bus itself is reopened.

## SCD41 (CO2)

Automatic self-calibration (`sensors.asc`) is off. It assumes the sensor
sees ~400 ppm fresh air at least once a week, which doesn't happen in a room
that's always occupied, so the baseline slowly drifts. Instead, take the
station outside and press **Calibrate CO2** on the Controls tab, once after
setup and then a few times a year. Target is `sensors.calibration_target_ppm`
(420). Give it 15 minutes outside before calibrating.

The collector refuses a calibration (`calibration_refused`) unless the
sensor has run for 3 minutes, has at least 3 readings from the last 5
minutes, those readings are within 30 ppm of each other, and they are within
200 ppm of the target. The sensor stores the result in its own EEPROM.

Other details:

- Altitude (`location.altitude_m`) is sent at start; once weather data is
  available, the live air pressure is sent instead (every 30 min, if it
  changed by 1 hPa or more).
- `sensors.scd41_temp_offset_c` only affects the SCD41's own temperature
  reading, not CO2. Its temperature and humidity are stored as `co2_temp` /
  `co2_humid`, so you can compare with the SHT41 and adjust:
  `new = T_scd41 - T_sht41 + old`.
- Runs in periodic mode (a new value every 5 s). The self-test (~10 s) runs
  only on the first open after the process starts; the result goes into the
  `sensor_init` event (`self_test`, `self_test_word`, `self_test_reason` =
  word / nack / crc).
- Offset, altitude and ASC are set again on every start rather than saved,
  to avoid EEPROM wear.

## SHT41 (temperature, humidity)

Factory calibrated. The heater is never used: every read is the
high-precision, no-heater command. The main error source is heat from the
Pi, so check it against a reference thermometer and set
`sensors.sht41_temp_offset_c` (usually negative).

## SPS30 (particulates)

Stores PM1, PM2.5, PM4, PM10, number concentrations and typical particle
size. AQI is calculated from PM2.5 only.

The built-in weekly fan clean is turned off and the collector runs it itself
on Sundays at 04:00 local time, so it shows up in the log. Readings are
skipped for the 15 s the fan runs at full speed. **Clean fan** on the
Controls tab does the same (at most once per 10 min). At debug log level the
collector also reads the status register (fan speed, laser, fan blocked)
after each read.

## Getting data out

- CSV: *Export CSV* on the History tab, or
  `/api/export.csv?from=<unix>&to=<unix>`.
- Everything (database, logs, journal): `make export` on the Pi.
