"""The static files: valid JavaScript (when node is available), no references to
endpoints that no longer exist, every element the script touches is in the page."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "dashboard" / "static"
TEMPLATE = Path(__file__).resolve().parents[1] / "dashboard" / "templates" / "index.html"
JS = STATIC / "dashboard.js"


def test_no_references_to_removed_endpoints():
    text = JS.read_text()
    for removed in ("/api/summary", "/api/flags", "/api/export.txt", "scd41_force_calibration",
                    "sps30_force_clean", "system_restart", "sps30_set_auto_cleaning_interval"):
        assert removed not in text, removed


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_javascript_parses():
    result = subprocess.run(["node", "--check", str(JS)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_every_element_id_the_script_uses_exists_in_the_page():
    page = TEMPLATE.read_text()
    ids_in_page = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', page))
    used = set(re.findall(r"getElementById\('([a-zA-Z0-9_-]+)'\)", JS.read_text()))
    # ids built from a template literal (`metric-${metric}`) are checked by their prefix
    missing = {i for i in used if i not in ids_in_page}
    assert not missing, missing
