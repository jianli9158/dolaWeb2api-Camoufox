"""Camoufox BiDi stream cleanup must abort its AbortController before the iterator closes."""

import asyncio
from unittest.mock import AsyncMock

from doubao2api.bidi_transport import BiDiClient
from doubao2api.browser_client import BrowserClient


def test_closing_stream_requests_abort_for_its_browser_request_id(tmp_path):
    async def run():
        client = BrowserClient(headless=True, user_data_dir=str(tmp_path))
        bidi = BiDiClient(AsyncMock(), AsyncMock())
        bidi.call_function = AsyncMock(return_value=None)
        client._bidi = bidi
        client._context_id = "ctx-1"
        client._ready = True
        client._sign_url = AsyncMock(return_value="/chat/completion")
        fetch_started = asyncio.Event()
        fetch_closed = asyncio.Event()

        async def blocked_fetch(url, payload, request_id, channel_name):
            fetch_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                fetch_closed.set()

        client._browser_fetch_stream = blocked_fetch
        stream = client.chat_completion("test")
        next_event = asyncio.create_task(stream.__anext__())
        await fetch_started.wait()
        next_event.cancel()
        try:
            await next_event
        except asyncio.CancelledError:
            pass

        assert bidi.call_function.await_count == 1
        ctx_id, abort_script, args = bidi.call_function.await_args.args
        assert ctx_id == "ctx-1"
        assert "c.abort()" in abort_script
        assert args[0]["value"].startswith("req_")
        assert not bidi._channels
        assert fetch_closed.is_set()

    asyncio.run(run())
