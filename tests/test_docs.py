"""The README stays true: every `make` target it names exists, every config key
it names is in config.toml, every path in "Where things live" exists."""

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
README = (REPO / "README.md").read_text()
MAKEFILE = (REPO / "Makefile").read_text()


def test_every_make_target_in_the_readme_exists():
    targets = set(re.findall(r"^([a-z][a-z-]*):", MAKEFILE, re.M))
    named = set(re.findall(r"(?:`|^)make ([a-z][a-z-]*)", README, re.M))
    assert named, "no make targets named in the README?"
    assert named <= targets, named - targets


def test_every_operator_and_agent_target_is_documented():
    documented = set(re.findall(r"(?:`|^)make ([a-z][a-z-]*)", README, re.M))
    public = {t for t in re.findall(r"^([a-z][a-z-]*):.*##", MAKEFILE, re.M)}
    assert public - {"agent-demo-stop"} <= documented, public - documented


def test_every_config_key_in_the_readme_exists_and_vice_versa():
    with open(REPO / "config.toml", "rb") as handle:
        config = tomllib.load(handle)
    keys = {f"{section}.{key}" for section, values in config.items() for key in values}
    named = set(re.findall(r"`([a-z_]+\.[a-z_0-9]+)`", README))
    named = {n for n in named if n.split(".")[0] in config}
    assert named <= keys, named - keys
    assert keys <= named, keys - named  # every knob explained


def test_where_things_live_matches_the_tree():
    block = README.split("## Where things live", 1)[1].split("```")[1]
    for line in block.splitlines():
        if not line.strip() or line[0] == " ":
            continue  # blank, or the continuation of a wrapped description
        name = line.split()[0]
        if name == "data/":
            continue  # git-ignored, created on the Pi
        assert (REPO / name.rstrip("/")).exists(), name


def test_event_types_named_in_the_readme_exist():
    from shared.events import EVENT_TYPES
    known = {t for types in EVENT_TYPES.values() for t in types}
    section = README.split("- The events you will see most", 1)[1].split("- A broken database", 1)[0]
    named = set(re.findall(r"`([a-z_]+)`", section))
    assert named <= known, named - known


def test_sensors_doc_uses_the_new_names():
    text = (REPO / "docs" / "sensors.md").read_text()
    assert "AIRMONITOR_" not in text and "export.txt" not in text and "rate guard" not in text
    for name in ("scd41_temp_offset_c", "sht41_temp_offset_c", "calibration_target_ppm", "asc"):
        assert f"`sensors.{name}`" in text, name
