# Sensors — how the collector cares for them

Everything here is about keeping the three Sensirion sensors truthful and
healthy. The numbers are constants next to the code (`collector/sensors.py`,
`collector/filters.py`); the four knobs a person may turn are in
`config.toml` under `[sensors]`. Source datasheets live in `datasheets/`.

## Warm-up, not burn-in

None of these sensors are metal-oxide types; the photoacoustic SCD41, the
capacitive SHT41 and the optical SPS30 need no conditioning period. They do
need a moment after every start: the SPS30 datasheet quotes 8–30 s until the
fan and laser are stable, and the SCD41's cell has to reach thermal
equilibrium. So after any start or re-init the collector waits **60 s for
the SCD41 and 30 s for the SPS30** (the SHT41 needs nothing): the cells stay
empty, one `warming_up` event is logged per sensor, and nothing counts
against the sensor. The bad-read streak and the silence timer start when the
warm-up ends. The panel shows "Warming up…" meanwhile.

## What is dropped

A value that cannot be air is stored as an empty cell, never as a number:
corrupt words (the SPS30's CRC, non-finite floats), negatives, temperatures
outside −40…85 °C, humidity outside 0…100 %, CO2 below 350 ppm (not indoor
air) or above the sensor's 40 000 ppm output range (a corrupt 0xFFFF). The
first drop of a streak logs a `value_dropped` event, then every sixth. Six
bad readings in a row, or two minutes without any reading, re-initialise
that sensor (`sensor_reinit`) with a growing delay from 30 s to 5 min; the
I2C bus itself is re-opened only when all sensors fail together.

## SCD41 — the CO2 sensor

- **Calibration is the important part.** Automatic self-calibration
  (`sensors.asc`) is **off** on purpose: it assumes the sensor sees fresh
  ~400 ppm air at least weekly, and in a continuously occupied room it slowly
  drags the baseline wrong. With it off, drift is corrected only by a
  **forced recalibration**: press **Calibrate CO2** on the Controls tab with
  the station in fresh outdoor air, once after installation and then a few
  times a year. The target is `sensors.calibration_target_ppm` (420). The
  collector refuses an unsafe one and logs `calibration_refused`: the sensor
  must have run 3 min, have at least 3 readings in the last 5 min, spread no
  more than 30 ppm, and sit within 200 ppm of the target. A successful one
  logs `calibration_done` with the correction the sensor reported and is
  stored under `last_calibration`; the Controls tab shows the checklist and
  the date of the last one. If the station ever moves somewhere regularly
  ventilated, `sensors.asc = true` is a valid choice.
- **Environment compensation.** At start the sensor gets
  `location.altitude_m`; as soon as the manager has a weather fetch, the
  collector sends the live surface pressure instead (every 30 min, only when
  it moved by 1 hPa or more). CO2 math is measurably wrong without it.
- **Temperature offset.** `sensors.scd41_temp_offset_c` (factory 4.0 °C) does
  not touch CO2 accuracy; it makes the SCD41's own temperature honest. The
  sensor's temperature and humidity are stored in every raw row (`co2_temp`,
  `co2_humid`) and charted next to the SHT41's in History, so the bench data
  says what the offset should be: with the room in equilibrium,
  `new = T_scd41 − T_sht41 + old`.
- **Single shot mode.** The sensor sits idle (0.15 mA) and measures only when
  the beat tells it to: two 5 s shots a minute, ~2.6 mA average instead of
  the 15 mA and twelve 200 mA pulses a minute of the default periodic mode
  on the Pi's 3.3 V pin — and the pulse lands at a known moment, five
  seconds into the beat, after the SHT41 has measured and well after the
  panel refresh. Less self-heating, so `co2_temp` reads lower than it did
  in periodic mode. The two shots during the 60 s warm-up are conditioning
  shots the datasheet asks to discard. A shot that yields nothing is
  silence: a `sensor_reinit` after 2 min without any value.
- Offset, altitude and ASC are re-applied on every start, not stored in the
  sensor. The one write to its EEPROM (rated ~2000 writes) is the
  calibration form's *Persist in sensor* box, which keeps the correction
  across power loss — a few times a year is nothing. A re-init gives the
  sensor the full 1 s soft-reset time before configuration is written; if a
  software re-init ever fails to unstick it, the datasheet's next step is a
  power cycle.

## SHT41 — temperature and humidity

- **Heater: off, and it cannot be left on.** The SHT4x heater exists to dry
  the sensor after condensation; it runs only when a heater command is sent
  and switches itself off within a second. Every measurement the collector
  sends is the no-heater high-precision command (the `sensor_init` event
  says `heater=off precision=high`). Indoors it is never needed.

Factory-calibrated, no user calibration exists. The real enemy is
self-heating from the Pi: measure against a reference thermometer and set
`sensors.sht41_temp_offset_c` (negative) to correct the mounting. The dew
point on the Live tab is computed from its values.

## SPS30 — particulates

- **What is stored:** mass PM1 / PM2.5 / PM10, number concentrations for
  0.5 / 1 / 2.5 µm, typical particle size. AQI is computed from PM2.5 only.
- **Self-diagnosis.** At `debug` level the collector reads the sensor's
  device status register (firmware ≥ 2.2) after every beat and writes the
  fan-speed, laser and fan-blocked bits into the debug sample line (`st_*`
  fields), so the bench log carries the sensor's own verdict next to the
  numbers it produced.
- **Fan hygiene.** The sensor's built-in weekly auto-clean is switched off and
  the collector runs the clean itself, **Sunday 04:00 local time**, so the
  event sits in the log and the blanked readings never enter the history
  (readings are dropped for 15 s while the fan runs at full speed). A manual
  **Clean fan** on the Controls tab does the same, at most once per 10 min.
  Sensirion's stated lifetime assumes the weekly clean happens; do not remove
  the schedule without a reason.

## Shutdown and start

On service stop the SCD41's periodic measurement is stopped and the SPS30's
measurement (and fan) is stopped, so power cycles and reboots never catch
the fan spinning or leave a sensor mid-command. Every start is therefore a
cold start for both sensors, hence the warm-up above. The collector's
`started` event says why it started (boot or restart, clean or killed
previous run) and its `shutdown` event carries the signal.

## Getting the numbers out

- **CSV:** the History tab's *Export CSV*, or
  `/api/export.csv?from=<unix>&to=<unix>` — every raw row in the range.
- **Everything:** `make export` on the Pi (database copy, logs, journal,
  system facts) — see the README.
