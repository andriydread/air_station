"""The page skeleton: five tabs, six buttons, no removed controls, RD chips, footer."""

import re

import pytest

from dashboard.app import create_app
from shared.events import Log


@pytest.fixture
def page(tmp_config, db):
    log = Log("dashboard", tmp_config, db=db, strict=True)
    app = create_app(tmp_config, db, log)
    html = app.test_client().get("/").get_data(as_text=True)
    log.close()
    return html


def test_five_tabs_in_order(page):
    tabs = re.findall(r'data-tab="([a-z]+)"', page)
    assert tabs == ["live", "history", "vitals", "diagnostics", "controls"]
    for name in tabs:
        assert f'id="tab-{name}"' in page


def test_exactly_six_command_buttons(page):
    commands = re.findall(r'data-command="([a-z0-9_]+)"', page)
    assert sorted(commands) == sorted([
        "scd41_calibrate", "sps30_fan_clean", "restart_collector", "restart_dashboard", "reboot", "delete_history",
    ])
    for removed in ("sps30_set_interval", "sps30_set_auto_cleaning_interval", "scd41_set_asc",
                    "display_redraw", "backup_now", "export.txt", "copy-text", "api/flags", "api/summary"):
        assert removed not in page, removed
    assert page.count('data-typed="') == 2  # reboot and delete history need a typed confirmation


def test_rd_chips_footer_and_charts(page):
    assert page.count('class="rd-chip"') == 3
    assert 'id="version-commit"' in page and 'id="warming-banner"' in page
    for chart in ("chart-nc", "chart-tps", "chart-cpu", "chart-load", "chart-mem", "chart-disk", "chart-wifi", "chart-lag"):
        assert f'id="{chart}"' in page
    assert 'id="event-app"' in page and "<option>watch</option>" in page
