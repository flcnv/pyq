import logging

import pytest

from src.main import warn_if_every_proxy_is_trusted


def test_warns_when_every_proxy_is_trusted(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)

    warn_if_every_proxy_is_trusted({"FORWARDED_ALLOW_IPS": "*"})

    [record] = caplog.records
    assert "spoofed" in record.message


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"FORWARDED_ALLOW_IPS": "10.0.0.0/16"},
        {"FORWARDED_ALLOW_IPS": "10.0.1.5,10.0.2.5"},
        {"FORWARDED_ALLOW_IPS": "10.0.1.5, *"},
    ],
    ids=[
        "unset (uvicorn default: localhost)",
        "subnet",
        "addresses",
        "* inside a list (uvicorn treats it as a literal)",
    ],
)
def test_no_warning_for_specific_addresses(
    environ: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)

    warn_if_every_proxy_is_trusted(environ)

    assert caplog.records == []
