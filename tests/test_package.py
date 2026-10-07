from importlib.metadata import version

import ov_reliability


def test_package_is_installed() -> None:
    assert ov_reliability.__doc__
    assert version("ov-reliability") == "0.1.0"
