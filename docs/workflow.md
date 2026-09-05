# How the station works, step by step

Three programs on the Pi, one database file. Each program is one loop on
one thread. They never talk to each other; they read and write the same
tables. Times are wall-clock; ":00 / :30" means the half-minute marks.

## Collector (reads the sensors)

1. **Boot.** systemd starts it; it waits up to 60 s for the clock to sync (the Pi has no clock battery).
2. **Open the I2C bus.**
3. **Start the SHT41** (temperature, humidity). Heater off. Ready at once.
4. **Start the SPS30** (dust). Fan on. 30 s warm-up.
5. **Start the SCD41** (CO2): sleep → wake → soft reset → **self-test** (10 s) → altitude 296 m, temperature offset, ASC off → idle. The self-test verdict is written into the `sensor_init` event. 60 s warm-up, during which two throw-away CO2 shots condition the sensor.
6. **Every 30 s, five seconds after :00 / :30**, one beat, in this order:
   1. SHT41: measure (8 ms) → temperature, humidity.
   2. SPS30: hand over the latest numbers (the fan runs all the time).
   3. SCD41: "measure once" → the sensor works 5 s (its 175 mA pulse happens now) → CO2, its own temperature and humidity.
   4. Check every number: corrupt word, negative, out of the sensor's range, CO2 below 350 → the cell is emptied and a `value_dropped` event is logged (first of a streak, then every 6th).
   5. Write **one row**: 12 numbers, stamped :00 or :30.
7. **After every beat, per sensor:** 6 bad readings in a row, **or** 6 bad readings inside 5 minutes, **or** 2 minutes without any reading → re-initialise that sensor (step 3/4/5 again; with a growing wait between attempts: 30 s → 5 min). All three failing at once → the bus is re-opened.
8. **Every 2 s:** look for a button press in the `commands` table (calibrate CO2, clean the dust fan). Run it, write the result.
9. **Every 30 s:** write the `collector_status` document (each sensor's health, re-init counts, calibration readiness).
10. **Every 30 min:** if the manager fetched weather, send the live air pressure to the SCD41 (only when it moved by 1 hPa or more).
11. **Sunday 04:00:** run the dust sensor's fan clean (10 s of full speed; dust readings are blanked for 15 s).
12. **Every 10 s:** tell systemd "alive". Wedged for 90 s → systemd restarts the program.
13. **Stop (deploy, reboot):** stop the SPS30 fan, leave the SCD41 idle, write the status document, exit.

## Manager (screen, weather, machine, housekeeping)

1. **Boot.** Wait for the clock to sync. Load the last forecast from the database so the first screen has weather.
2. **Open SPI**, reset the e-paper.
3. **Fetch the weather** (Open-Meteo) once at start, then every 30 min; on failure retry every 2 min. Store it as `last_weather`.
4. **Every minute at :00:**
   1. Read the two rows of the minute that ended one beat ago (:00 of the previous minute and :30) and average them.
   2. Compute: AQI from PM2.5 (six EPA words), CO2 word (Good / Elevated / Poor at 1000 / 2000), the three 3-hour weather blocks, the glyphs (Wi-Fi, power, sensor), the "warming up" and "collector silent" flags.
   3. Write the `display_data` document (the Live tab reads this).
   4. Draw the picture, send it to the panel: partial refresh; a full refresh every 5 min.
   5. Check the collector: no row for 5 min → restart it (once per 10 min).
5. **Every 30 s:** ping the router and the internet. Router unreachable 6 times in a row (3 min) → Wi-Fi radio off/on (`wifi_bounce`, with what the network stack saw); at most once per 10 min.
6. **Every minute:** write a `vitals` row: CPU temperature, load, memory, disk, database size, Wi-Fi signal, ping times, under-voltage flag, uptime, how late the collector's last row is.
7. **Every 30 s:** write the `manager_status` document.
8. **Every 2 s:** look for a button press meant for the manager (restart collector, restart dashboard, reboot). Run it via a fixed `sudo` command, write the result.
9. **On the hour:** fold the raw rows of the finished hour into one `hourly_measurements` row (min / max / average / count). Missed hours are caught up at start.
10. **00:05 local:** prune old rows (raw 90 days, vitals / events / commands 30 days), checkpoint the database, copy it to `airstation.db.bak`.
11. **Every 10 s:** tell systemd "alive".
12. **Stop:** write the status document, put the panel to sleep, exit.

## Dashboard (the web page)

1. **Boot.** Start the web server on port 8080. No hardware.
2. **Browser opens the page** → it gets the HTML, the script, the style once.
3. **Every 10 s the browser asks `/api/changes`:** one small answer with the timestamps of the state documents and the newest row. Nothing changed → nothing else is fetched.
4. **Something changed → fetch the tab that is open:**
   - Live: `display_data` (the averages), `collector_status`, `manager_status`; dew point is computed in the browser.
   - History: `/api/history` for the chosen range → raw rows bucketed (30 s up to 2 h, 1 min up to 6 h, … ) or hourly rows beyond 90 days; CSV export of the same range.
   - Vitals: the `vitals` rows for the range.
   - Diagnostics: events, restarts, the status documents, a preview of the panel image.
   - Controls: six buttons.
5. **A button press** → `POST /api/commands` → one row in the `commands` table (status *queued*) → the collector or the manager picks it up within 2 s → status *running* → *success* or *fail* with the result → the page polls every second for 15 s to show it.
6. **Every 10 s:** tell systemd "alive".

## What the database holds

| table | written by | one row per |
|---|---|---|
| `raw_measurements` | collector | beat (30 s), 12 numbers |
| `hourly_measurements` | manager | hour, min / max / avg / count |
| `vitals` | manager | minute, machine health |
| `events` | all three | something worth a line: starts, re-inits, drops, Wi-Fi, nightly |
| `commands` | dashboard → collector / manager | button press, with its result |
| `state` | collector, manager | `collector_status`, `manager_status`, `display_data`, `last_weather` |

## Findings so far (2026-09-05, from two exports)

- The Pi, the database, the panel, the dust and the temperature sensors: all healthy. Wi-Fi was weak (−80 dBm) and dropped once for 13 h; after the reboot it sits at −62 … −74 dBm and is stable.
- The 5 V side never dipped (the Pi's own under-voltage flag was 0 the whole time).
- The **CO2 sensor** has two states. *Working:* smooth values, no zeros, reacts to the room. *Failed:* CO2 = 0 on many readings and a flat ~460 in between, while its own temperature and humidity stay correct. It goes from working to failed after some hours of running; a one-second soft re-init does not bring it back; a reboot (which stops it for 1–2 min) does, for a few hours.
- The station's beat is now 30 s, the sensors fire one after another, the CO2 sensor rests between shots, and it self-tests on every start. The next export shows the self-test verdict during a failure.
- Open question: with the windows open 24/7 in a 40–50 m² room, the "working" 1200–1300 is high for the room and the "failed" ~460 is a normal value — so neither state is trusted until the sensor is checked outdoors (see `dev/bench-2026-09-05.md` §7).
