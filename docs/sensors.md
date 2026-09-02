# Sensors and data — reference

Details moved out of the README so the README can stay short. Everything here is
about keeping the three Sensirion sensors truthful and healthy, and about getting
data off the Pi. Source datasheets live in `datasheets/`.

## Sensor care (what the collector does for the hardware)

What the station does (and deliberately doesn't do) to keep the sensors
truthful and healthy:

- **Burn-in: not required, warm-up: yes.** None of these sensors are
  metal-oxide (MOX) types; photoacoustic (SCD41), capacitive (SHT41) and
  optical (SPS30) elements need no conditioning period. They do need a
  moment after every start: the SPS30 datasheet quotes 8–30 s until the
  fan/laser output is stable, and the SCD41's photoacoustic cell has to
  reach thermal equilibrium (Sensirion has the master discard the first
  readings after power-up). Readings inside `AIRMONITOR_SCD41_WARMUP_SECONDS`
  / `AIRMONITOR_SPS30_WARMUP_SECONDS` of a start are stored as **flagged**
  samples (raw value kept, not averaged, visible in Diagnostics and the
  text export) and never become the rate guard's baseline. Out-of-range
  words (below `AIRMONITOR_MIN_VALID_CO2_PPM` or above the sensor's
  40'000 ppm output range, e.g. a corrupt 0xFFFF) are rejected outright.
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
- **SCD41 environment compensation**: altitude and the RH/T offset are
  written to the sensor (in idle mode, as the datasheet requires) before
  every measurement start — CO2 math is measurably wrong without the
  altitude. The offset (`AIRMONITOR_SCD41_TEMP_OFFSET`, factory 4.0 °C)
  does not touch CO2 accuracy; it only makes the SCD41's own temperature
  comparable to the SHT41's for the cross-check. To tune it: with the
  station in thermal equilibrium, `new = T_scd41 − T_reference + old`.
  Neither value is persisted to the sensor's EEPROM (rated ~2000 writes);
  the collector re-applies them on every start instead.
- **SCD41 recovery follows the datasheet timings**: after `reinit` the
  sensor gets the full 1 s soft-reset time before configuration is
  written (the Adafruit driver waits 20 ms, which could turn a recovery
  into "re-initialization failed"). If a software reinit ever fails to
  unstick the sensor, the datasheet's next step is a power cycle — the
  station cannot do that itself.
- **SHT41**: factory-calibrated, no user calibration exists. The real
  enemy is self-heating from the Pi; measure against a reference
  thermometer and set `AIRMONITOR_SHT41_TEMP_OFFSET` (negative) to
  correct the mounting. Its readings are cross-checked against the
  SCD41's internal sensors — sustained disagreement raises a
  `sensor_disagreement` event, catching silent drift no single sensor
  can self-report.
- **SPS30 self-diagnosis**: every minute the collector reads the sensor's
  Device Status Register (firmware ≥ 2.2): a blocked/broken fan, a laser
  current fault or an out-of-range fan speed marks the SPS30 unhealthy
  (dashboard pill, e-paper glyph) with a `device_status` event — the
  sensor's own verdict, not a guess from the numbers. Older firmware logs
  one `status_unsupported` note and skips the check.
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
  mid-command. Consequence: every restart is a cold start for both
  sensors, hence the warm-up flags above.
- **Every start is explained.** The collector's `started` event says
  whether the Pi rebooted (kernel boot id), how long the station was
  silent, whether the previous run shut down cleanly or was killed
  (watchdog, crash, power loss) and what triggered it (a dashboard
  reboot/restart command, the systemd watchdog, a deploy). `shutdown`
  events carry the signal. When a reading looks odd, the text export
  (below) shows these lines next to the numbers.

## Getting data out

- **CSV** (`Export CSV` on the History tab, or `/api/export.csv?hours=24`):
  every raw 10 s sample in the range, flags included — for spreadsheets.
- **Text** (`Copy as text` / `Open as text`, or `/api/export.txt`): a
  paste-sized table (≈150 rows, bucket chosen automatically) with station
  events and flagged samples interleaved by time — made for handing a
  slice of history to a person or a chat model. Options:
  `?hours=6`, `?from=…&to=…`, `?metrics=co2,temp`, `?bucket=300`.
  From a laptop straight into the clipboard:

  ```
  curl -s "http://pizero.local:8080/api/export.txt?hours=6&metrics=co2" | pbcopy            # macOS
  curl -s "http://pizero.local:8080/api/export.txt?hours=6&metrics=co2" | xclip -sel clip   # Linux
  curl.exe -s "http://pizero.local:8080/api/export.txt?hours=6&metrics=co2" | clip          # Windows
  ```
