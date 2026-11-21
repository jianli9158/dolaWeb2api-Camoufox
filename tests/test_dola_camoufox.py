"""Tests for Dola International protocol, Camoufox BiDi runtime, proxy relay, redaction, cooldowns, and Web LiveView."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from doubao2api.account_manager import AccountManager
from doubao2api.bidi_transport import (
    BiDiClient,
    LocalSocks5Relay,
    _from_bidi_remote_value,
    _to_bidi_local_value,
)
from doubao2api.browser_client import BrowserClient
from doubao2api.camoufox_liveview import CamoufoxLiveView
from doubao2api.camoufox_profile import ProfileLeaseBusyError, acquire_profile_lease
from doubao2api.dola_config import (
    DOLA_CHAT_URL,
    DOLA_DEFAULT_BOT_ID,
    DOLA_PC_VERSION,
    DOLA_URL,
    DOLA_WEB_AID,
)
from doubao2api.proxy_utils import mask_proxy_url, normalize_proxy_url, proxy_needs_relay
from doubao2api.redaction import redact_headers, redact_sensitive_text, redact_structure
from doubao2api.unified_server import create_app


def test_dola_query_params_and_headers(tmp_path):
    client = BrowserClient(headless=True, user_data_dir=str(tmp_path))
    client._device_id = "7710545052401403445"
    client._web_id = "7710545044447446533"
    client._region = "JP"
    client._language = "en"

    params = client._build_query_params()
    assert params["aid"] == str(DOLA_WEB_AID)
    assert params["real_aid"] == str(DOLA_WEB_AID)
    assert params["pc_version"] == DOLA_PC_VERSION
    assert params["region"] == "JP"
    assert params["sys_region"] == "JP"
    assert params["language"] == "en"
    assert params["device_id"] == "7710545052401403445"

    headers = client._build_headers("sessionid=abc; passport_csrf_token=csrf123")
    assert headers["Origin"] == DOLA_URL
    assert headers["Referer"] == DOLA_CHAT_URL
    assert headers["x-tt-passport-csrf-token"] == "csrf123"
    assert DOLA_DEFAULT_BOT_ID == "7339470689562525703"


def test_bidi_value_codec_and_channel_streaming():
    payload = {"ok": True, "n": 42, "items": ["a", None, {"k": "v"}]}
    encoded = _to_bidi_local_value(payload)
    assert _from_bidi_remote_value(encoded) == payload

    async def _run():
        bidi = BiDiClient(AsyncMock(), AsyncMock())
        ch = bidi.open_channel("stream_1")
        bidi._handle_message(
            json.dumps(
                {
                    "method": "script.message",
                    "params": {
                        "channel": "stream_1",
                        "data": {"type": "string", "value": '{"text":"hello"}'},
                    },
                }
            )
        )
        chunk = await ch.next_text(timeout=1.0)
        assert json.loads(chunk) == {"text": "hello"}
        ch.close()
        assert "stream_1" not in bidi._channels

    asyncio.run(_run())


def test_proxy_normalization_relay_and_masking():
    assert normalize_proxy_url("st5 1.2.3.4:1080:alice:secret") == "socks5://alice:secret@1.2.3.4:1080"
    assert normalize_proxy_url("1.2.3.4:8080:bob:p@ss:1") == "http://bob:p@ss:1@1.2.3.4:8080"
    assert normalize_proxy_url("127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert proxy_needs_relay("http://alice:secret@1.2.3.4:8080") is True
    assert proxy_needs_relay("socks5://127.0.0.1:1080") is True
    assert proxy_needs_relay("http://127.0.0.1:7890") is False
    assert mask_proxy_url("socks5://alice:secret@1.2.3.4:1080") == "socks5://alice:***@1.2.3.4:1080"

    with pytest.raises(ValueError):
        normalize_proxy_url("not-a-valid-proxy")


def test_local_socks5_relay_starts_and_closes():
    async def _run():
        relay = LocalSocks5Relay("http://user:pass@127.0.0.1:9999")
        host, port = await relay.start()
        assert host == "127.0.0.1"
        assert port > 0
        await relay.close()

    asyncio.run(_run())


def test_profile_kernel_lease_prevents_double_acquire(tmp_path):
    lease = acquire_profile_lease(tmp_path, holder="worker-1")
    try:
        with pytest.raises(ProfileLeaseBusyError):
            acquire_profile_lease(tmp_path, holder="worker-2", wait_seconds=0.05)
    finally:
        lease.release()

    # Re-acquiring succeeds after release
    lease2 = acquire_profile_lease(tmp_path, holder="worker-2")
    lease2.release()


def test_redaction_scrubs_dola_secrets_and_proxies():
    raw = (
        "Authorization: Bearer sk-secret-token-123456; "
        "sessionid=my_secret_session_value; "
        "proxy=socks5://alice:topsecret@10.0.0.1:1080 "
        "url=https://www.dola.com/chat/completion?a_bogus=abc123&msToken=tok999"
    )
    scrubbed = redact_sensitive_text(raw)
    assert "sk-secret-token-123456" not in scrubbed
    assert "my_secret_session_value" not in scrubbed
    assert "topsecret" not in scrubbed
    assert "abc123" not in scrubbed
    assert "tok999" not in scrubbed

    hdrs = redact_headers({"Cookie": "sessionid=abc", "x-tt-passport-csrf-token": "csrf", "Origin": DOLA_URL})
    assert hdrs["Cookie"] == "<redacted>"
    assert hdrs["x-tt-passport-csrf-token"] == "<redacted>"
    assert hdrs["Origin"] == DOLA_URL

    nested = redact_structure({"cookies": {"sessionid": "xyz"}, "safe": "ok"})
    assert nested["cookies"] == "<redacted>"
    assert nested["safe"] == "ok"


def test_account_per_capability_cooldown_and_failover(tmp_path):
    am = AccountManager(filepath=str(tmp_path / "accounts.json"), strategy="failover")
    a1 = am.add_or_update_account("Dola-1", {"sessionid": "s1"}, account_id="a1", proxy_url="st5 1.2.3.4:1080:u:p")
    a2 = am.add_or_update_account("Dola-2", {"sessionid": "s2"}, account_id="a2")
    am.set_active_account(a1.id)

    assert a1.proxy_url == "socks5://u:p@1.2.3.4:1080"
    assert a1.to_dict(mask_cookies=True)["proxy_masked"] == "socks5://u:***@1.2.3.4:1080"

    # Chat cooldown skips a1 for chat, while a1 remains eligible for video
    am.mark_cooldown(a1.id, feature="chat", seconds=120.0)
    assert am.get_next_available_account("chat").id == a2.id
    assert am.get_next_available_account("video").id == a1.id

    am.clear_cooldowns(a1.id)
    assert am.get_next_available_account("chat").id == a1.id


def test_admin_liveview_and_proxy_endpoints(tmp_path):
    client = BrowserClient(headless=True, user_data_dir=str(tmp_path))
    client._ready = True
    client.liveview = CamoufoxLiveView()
    client.liveview.frame_b64 = "ZmFrZQ=="
    client.liveview.frame_seq = 1
    client.liveview.frame_meta = {"width": 1280, "height": 800, "url": DOLA_CHAT_URL}
    client.set_proxy = AsyncMock(side_effect=lambda p: setattr(client, "_proxy_url", p) or p)

    app = create_app(browser_client=client)
    tc = TestClient(app)

    # LiveView snapshot & input
    snap = tc.get("/admin/api/liveview/snapshot")
    assert snap.status_code == 200
    assert snap.json()["frame_b64"] == "ZmFrZQ=="

    inp = tc.post("/admin/api/liveview/input", json={"type": "pointer", "action": "click", "x": 100, "y": 200})
    assert inp.status_code == 200
    assert len(client.liveview._inputs) == 1

    # Proxy config GET/POST
    p_set = tc.post("/admin/api/config/proxy", json={"proxy_url": "st5 1.2.3.4:1080:alice:secret"})
    assert p_set.status_code == 200
    assert p_set.json()["proxy_masked"] == "socks5://alice:***@1.2.3.4:1080"
    assert "secret" not in p_set.text

    p_bad = tc.post("/admin/api/config/proxy", json={"proxy_url": "invalid-proxy"})
    assert p_bad.status_code == 400


def test_isolated_account_profile_dirs_and_update(tmp_path):
    am = AccountManager(storage_dir=str(tmp_path), strategy="failover")
    a1 = am.add_or_update_account("Acc1", {"sessionid": "s1"}, account_id="acc_one", proxy_url="1.2.3.4:8080:u1:p1")
    a2 = am.add_or_update_account("Acc2", {"sessionid": "s2"}, account_id="acc_two")

    d1 = am.resolve_account_dir(a1.id)
    d2 = am.resolve_account_dir(a2.id)
    assert d1 != d2
    assert d1.endswith("accounts/acc_one")
    assert d2.endswith("accounts/acc_two")

    updated = am.update_account(a2.id, name="Acc2-JP", proxy_url="st5 9.8.7.6:1080:bob:pw")
    assert updated.name == "Acc2-JP"
    assert updated.proxy_url == "socks5://bob:pw@9.8.7.6:1080"

    # Deleting with delete_profile=True cleans isolated directory
    from pathlib import Path
    p2 = Path(d2) / "firefox-profile"
    p2.mkdir(parents=True, exist_ok=True)
    (p2 / "prefs.js").write_text("user_pref();", encoding="utf-8")
    assert am.delete_account(a2.id, delete_profile=True) is True
    assert not Path(d2).exists()


def test_seedream_image_and_seedance_video_chat_ability(tmp_path):
    client = BrowserClient(headless=True, user_data_dir=str(tmp_path))
    captured = []

    async def fake_chat_completion(text, image_attachments=None, use_deep_think=0, chat_ability=None, **kwargs):
        captured.append({"text": text, "chat_ability": chat_ability, "images": image_attachments})
        if chat_ability and chat_ability.get("ability_type") == 17:
            yield {
                "patch_op": [
                    {
                        "patch_value": {
                            "content_block": [
                                {
                                    "block_type": 2074,
                                    "content": {
                                        "creation_block": {
                                            "creations": [
                                                {
                                                    "video": {
                                                        "video_url": "https://cdn.dola.com/seedance_out.mp4",
                                                        "cover_url": "https://cdn.dola.com/cover.jpg",
                                                    },
                                                    "duration": 10,
                                                }
                                            ]
                                        }
                                    },
                                }
                            ]
                        }
                    }
                ]
            }
        else:
            yield {
                "patch_op": [
                    {
                        "patch_value": {
                            "content_block": [
                                {
                                    "block_type": 2074,
                                    "content": {
                                        "creation_block": {
                                            "creations": [
                                                {
                                                    "image": {
                                                        "key": "img_out_1",
                                                        "image_ori": {"url": "https://cdn.dola.com/seedream_out.png", "width": 1024, "height": 1024},
                                                    }
                                                }
                                            ]
                                        }
                                    },
                                }
                            ]
                        }
                    }
                ]
            }

    client.chat_completion = fake_chat_completion

    async def _run():
        img_res = await client.generate_image("A fox", model="seedream-5.0-pro", ratio="16:9")
        assert img_res["model"] == "Seedream 5.0 Pro"
        assert img_res["images"][0]["url"] == "https://cdn.dola.com/seedream_out.png"
        img_ab = json.loads(captured[0]["chat_ability"]["ability_param"])
        assert captured[0]["chat_ability"]["ability_type"] == 3
        assert img_ab["ability_param"]["model"] == "Seedream 5.0 Pro"
        assert img_ab["ability_param"]["ratio"] == "16:9"

        vid_res = await client.generate_video("Ocean waves", model="seedance-2.0-fast", ratio="16:9", duration=5)
        assert vid_res["model"] == "seedance_v2.0"
        assert vid_res["videos"][0]["video_url"] == "https://cdn.dola.com/seedance_out.mp4"
        vid_ab = json.loads(captured[1]["chat_ability"]["ability_param"])
        assert captured[1]["chat_ability"]["ability_type"] == 17
        assert vid_ab["model"] == "seedance_v2.0"
        assert vid_ab["duration"] == 5
        assert vid_ab["ratio"] == "16:9"

    asyncio.run(_run())


def test_models_catalog_and_account_update_api(tmp_path):
    client = BrowserClient(headless=True, user_data_dir=str(tmp_path))
    client._ready = True
    acc = client.account_manager.add_or_update_account("DolaMain", {"sessionid": "s1"}, account_id="acc_main")

    app = create_app(browser_client=client)
    tc = TestClient(app)

    # Verify discovered Seedream & Seedance models appear in /v1/models
    models = [m["id"] for m in tc.get("/v1/models").json()["data"]]
    for expected in ("seedream-4.5", "seedream-5.0-pro", "seedance-2.0-fast", "seedance-2.0", "seedance-1.0"):
        assert expected in models

    # Update account name & per-account proxy via PUT /admin/api/accounts/{id}
    upd = tc.put(
        f"/admin/api/accounts/{acc.id}",
        json={"name": "Dola-US", "proxy_url": "st5 5.6.7.8:1080:alice:secret"},
    )
    assert upd.status_code == 200
    assert upd.json()["account"]["name"] == "Dola-US"
    assert upd.json()["account"]["proxy_masked"] == "socks5://alice:***@5.6.7.8:1080"
    assert "secret" not in upd.text

    # Validation on login-sessions/start
    bad_mode = tc.post("/admin/api/login-sessions/start", json={"mode": "bogus"})
    assert bad_mode.status_code == 400
    bad_proxy = tc.post("/admin/api/login-sessions/start", json={"mode": "add", "proxy_url": "not-a-proxy"})
    assert bad_proxy.status_code == 400
    missing_acc = tc.post("/admin/api/login-sessions/start", json={"mode": "manage", "account_id": "nope"})
    assert missing_acc.status_code == 404
