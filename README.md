# Air Station

Indoor air-quality station on a Raspberry Pi Zero 2 W: SCD41 (CO2), SHT41
(temperature/humidity), SPS30 (particulates), a 3.7" e-paper panel, and a web
dashboard.

**Rewrite in progress.** The code is being rebuilt as three programs
(collector, manager, dashboard) sharing one SQLite database. Until the rewrite
lands, this tree does not run; the station keeps running the previous version,
commit `ace2686`.

What is here now:

- `drivers/` — the hand-written SPS30 (I2C) and UC8253C e-paper (SPI) drivers, unchanged.
- `datasheets/` — the Sensirion datasheets the drivers and filters follow.
- `assets/` — the panel font and weather icons.
- `docs/sensors.md` — sensor care notes (written for the previous version).
- `systemd/` — the Wi-Fi power-save and hardware-watchdog helpers.

Developing off the Pi: `make agent-venv` then `make agent-test`.
