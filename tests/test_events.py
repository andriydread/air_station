"""The logger: line format, levels, events rows, strictness, failure counting."""

import io
import logging

import pytest

from shared.events import EVENT_TYPES, Log, format_line, format_value, git_commit, is_known_event

from datetime import datetime, timezone

T = int(datetime(2026, 9, 3, 12, 0, 10, tzinfo=timezone.utc).timestamp())  # 2026-09-03T12:00:10Z


def _lines(log):
    for handler in log._logger.handlers:
        handler.flush()
    return log.path.read_text(encoding="utf-8").splitlines()


def test_line_format_is_exact():
    line = format_line(T, "debug", "collector", "scd41", "sample",
                       {"co2": 812, "temp": 23.41, "ok": True, "warmup_left": 0, "err": None})
    assert line == "2026-09-03T12:00:10Z DEBUG collector scd41 sample co2=812 temp=23.41 ok=1 warmup_left=0 err=-"
    assert format_line(T, "info", "manager", "app", "start") == "2026-09-03T12:00:10Z INFO manager app start"
    assert format_line(T, "info", "manager", "app", "two words") .endswith("two_words")


def test_values_with_spaces_equals_or_quotes_are_quoted():
    assert format_value("plain") == "plain"
    assert format_value("has space") == '"has space"'
    assert format_value("a=b") == '"a=b"'
    assert format_value('say "hi"') == '"say \\"hi\\""'
    assert format_value("") == '""'
    assert format_value(False) == "0"
    assert format_value(1.5) == "1.5"
    assert format_value({"b": 1, "a": [1, 2]}) == '"{\\"a\\":[1,2],\\"b\\":1}"'


def test_level_filtering_and_file_location(tmp_config, tmp_path):
    log = Log("collector", tmp_config, clock=lambda: T)
    assert log.path == tmp_path / "data" / "logs" / "collector.log"
    log.debug("scd41", "sample", co2=1)
    log.info("app", "hello")
    assert [l.split(" ", 2)[1] for l in _lines(log)] == ["DEBUG", "INFO"]
    log.close()

    quiet = tmp_config.__class__.from_dict(
        {**_raw_of(tmp_config), "logging": {"level": "info", "i2c_trace": False}},
        repo_root=tmp_config.repo_root, source=tmp_config.source,
    )
    log = Log("manager", quiet, clock=lambda: T)
    log.debug("display", "frame")
    log.info("display", "frame")
    assert len(_lines(log)) == 1
    log.close()


def _raw_of(config):
    data = config.as_dict()
    return {
        "location": data["location"], "sensors": data["sensors"],
        "retention_days": data["retention_days"], "weather": data["weather"],
        "dashboard": data["dashboard"], "paths": data["paths"], "logging": data["logging"],
    }


def test_rotation_handler_uses_the_retention_count(tmp_config):
    log = Log("dashboard", tmp_config)
    handler = log._file
    assert handler.backupCount == tmp_config.retention_days.logs == 45
    assert handler.when == "MIDNIGHT" and handler.utc is True
    log.close()


def test_event_writes_a_line_and_a_row(tmp_config, db):
    log = Log("collector", tmp_config, db=db, clock=lambda: T)
    log.event("warning", "scd41", "value_dropped", "co2 out of range", value=0, reason="range")
    line = _lines(log)[-1]
    assert line.startswith("2026-09-03T12:00:10Z WARNING collector scd41 value_dropped ")
    assert 'msg="co2 out of range"' in line and "value=0" in line and "reason=range" in line
    row = db.recent_events()[0]
    assert (row["app"], row["level"], row["source"], row["type"], row["ts"]) == (
        "collector", "warning", "scd41", "value_dropped", T)
    assert row["details"] == {"value": 0, "reason": "range"}
    log.close()


def test_strict_logger_rejects_unknown_types_and_production_warns(tmp_config, db):
    strict = Log("manager", tmp_config, db=db, strict=True)
    with pytest.raises(ValueError, match="unknown event type weather.rainbow"):
        strict.event("info", "weather", "rainbow", "x")
    with pytest.raises(ValueError, match="level"):
        strict.event("debug", "weather", "weather_error", "x")
    strict.close()

    lax = Log("manager", tmp_config, db=db, clock=lambda: T)
    lax.event("info", "weather", "rainbow", "x")
    assert lax.unknown_events == 1
    assert any("unknown_event_type" in line for line in _lines(lax))
    assert db.recent_events()[0]["type"] == "rainbow"  # still recorded — the fact matters more than the name
    lax.close()


def test_vocabulary_is_complete():
    for source in ("scd41", "sht41", "sps30", "i2c"):
        assert "sensor_reinit" in EVENT_TYPES[source]
    assert is_known_event("app", "clock_jump") and is_known_event("web", "server_error")
    assert not is_known_event("app", "server_error")
    assert sum(len(v) for v in EVENT_TYPES.values()) >= 35


def test_database_failure_is_counted_not_raised(tmp_config):
    class BrokenDb:
        def insert_event(self, *_a, **_k):
            raise RuntimeError("disk full")

    log = Log("collector", tmp_config, db=BrokenDb())
    log.event("info", "app", "started", "x")
    assert log.db_failures == 1 and log.failures == 1
    log.close()


def test_log_write_failure_is_counted_not_raised(tmp_config, monkeypatch):
    log = Log("collector", tmp_config)

    def boom(_record):
        raise OSError("disk full")

    monkeypatch.setattr(log._file, "emit", lambda record: log._file.handleError(record))
    log.info("app", "x")
    assert log.failures == 1
    log.close()


def test_exception_line_carries_the_traceback_and_an_event(tmp_config, db):
    log = Log("manager", tmp_config, db=db, clock=lambda: T)
    try:
        raise KeyError("missing")
    except KeyError:
        log.exception("display", "frame failed", frame=3)
    lines = _lines(log)
    assert any('traceback="Traceback' in line and "KeyError" in line and "frame=3" in line for line in lines)
    row = db.recent_events()[0]
    assert row["type"] == "error" and row["source"] == "app" and row["details"]["origin"] == "display"
    assert "KeyError" in row["details"]["exc"]
    log.close()


def test_start_line_and_stderr_gets_warnings_only(tmp_config):
    stream = io.StringIO()
    log = Log("collector", tmp_config, stream=stream, clock=lambda: T)
    log.start_line(tmp_config, commit="abc1234")
    log.warning("app", "careful")
    line = _lines(log)[0]
    assert " INFO collector app start commit=abc1234 python=" in line and 'config="{' in line
    assert "careful" in stream.getvalue() and "start" not in stream.getvalue()
    log.close()


def test_git_commit_reads_head(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef\n")
    assert git_commit(tmp_path) == "0123456"
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs\nfedcba9876543210 refs/heads/main\n")
    assert git_commit(tmp_path) == "fedcba9"
    assert git_commit(tmp_path / "nowhere") == "-"


def test_unknown_app_is_refused(tmp_config):
    with pytest.raises(ValueError):
        Log("robot", tmp_config)
