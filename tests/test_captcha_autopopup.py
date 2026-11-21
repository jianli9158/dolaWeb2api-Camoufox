"""Tests for captcha risk tracking and Camoufox mode switching."""

import asyncio
from unittest.mock import AsyncMock, patch
from doubao2api.browser_client import BrowserClient


def test_record_failure_risk_codes():
    client = BrowserClient(headless=True)
    client._ready = True

    client.record_failure(710022002)
    assert client.needs_captcha is True
    assert client.last_error_code == 710022002
    assert client.is_ready is True

    for _ in range(10):
        client.record_failure(710022002)
    assert client.is_ready is True

    client.clear_captcha()
    assert client.needs_captcha is False
    assert client.consecutive_failures == 0

    client.record_failure(710022004)
    assert client.needs_captcha is False

    client.record_failure(710022004, verification_required=True)
    assert client.needs_captcha is True


def test_auto_popup_for_captcha():
    async def _run():
        client = BrowserClient(headless=True)
        client.switch_mode = AsyncMock(return_value=True)
        with patch("doubao2api.browser_client.has_display", return_value=True):
            popped = await client.auto_popup_for_captcha()
        assert popped is True
        client.switch_mode.assert_awaited_once_with(headless=False)

    asyncio.run(_run())


def test_switch_mode_reports_open_unlogged_camoufox_as_available():
    async def _run():
        client = BrowserClient(headless=True)
        client._ready = False

        async def restart():
            client._ready = False

        client.restart = AsyncMock(side_effect=restart)
        client.is_alive = AsyncMock(return_value=True)

        with patch("doubao2api.browser_client.has_display", return_value=True):
            available = await client.switch_mode(headless=False)

        assert available is True
        assert client.is_ready is False
        assert client.headless is False
        client.restart.assert_awaited_once()

    asyncio.run(_run())
