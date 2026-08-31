"""Everything must be importable off-Pi with the fake hardware modules."""


def test_all_app_modules_import():
    import main  # noqa: F401
    import airmonitor.commands  # noqa: F401
    import airmonitor.config  # noqa: F401
    import airmonitor.logging_utils  # noqa: F401
    import airmonitor.network  # noqa: F401
    import airmonitor.sensors  # noqa: F401
    import airmonitor.storage  # noqa: F401
    import dashboard.app  # noqa: F401
    import lib.sps30_i2c  # noqa: F401
    import lib.uc8253c  # noqa: F401
    import utils.aqi  # noqa: F401
    import utils.display  # noqa: F401
    import utils.weather  # noqa: F401


def test_config_defaults_validate():
    from airmonitor.config import Config

    Config().validate()


def test_dashboard_import_is_side_effect_free():
    """Importing dashboard.app must not configure logging or create the DB;
    the app is only built by create_app() (tests) or the __main__ block."""
    import dashboard.app as dashboard_app

    assert not hasattr(dashboard_app, "app")
