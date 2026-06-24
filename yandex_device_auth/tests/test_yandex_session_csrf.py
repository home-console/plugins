from __future__ import annotations

import pytest

from plugins.yandex_device_auth.yandex_session import _extract_csrf


def test_extract_csrf_legacy_input() -> None:
    html = '<input name="csrf_token" value="abc123def456" />'
    assert _extract_csrf(html) == "abc123def456"


def test_extract_csrf_window_global() -> None:
    html = 'window.__CSRF__ = "3404d9c9b06bd2c959f90f4d4d79b4c03e213816:1782332676";'
    assert _extract_csrf(html).startswith("3404d9c9")


def test_extract_csrf_missing_raises() -> None:
    with pytest.raises(ValueError, match="csrf_token"):
        _extract_csrf("<html><title>Log in</title></html>")
