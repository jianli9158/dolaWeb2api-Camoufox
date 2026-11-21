"""
Tests for Img2Img (image edits) and Img2Video (video generations with reference image).
"""
import base64
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi.testclient import TestClient
from doubao2api.unified_server import create_app


@pytest.fixture
def mock_browser_client():
    client = MagicMock()
    client.is_ready = True
    client.needs_captcha = False
    client.consecutive_failures = 0
    client.last_error_code = None
    client.headless = True
    client.browser_name = "chromium"

    client.is_alive = AsyncMock(return_value=True)
    client.upload_image = AsyncMock(return_value={
        "uri": "tos-cn-i-mock/ref_image_123.png",
        "cdn_url": "https://tos.mock.com/ref_image_123.png",
        "name": "ref_image.png",
        "format": "png",
    })
    client.generate_image = AsyncMock(return_value={
        "images": [{"url": "https://tos.mock.com/generated_img.png"}],
    })
    client.generate_video = AsyncMock(return_value={
        "videos": [{"video_url": "https://tos.mock.com/generated_vid.mp4", "duration": 5}],
    })
    return client


@pytest.fixture
def test_app(mock_browser_client):
    app = create_app(browser_client=mock_browser_client)
    yield app, mock_browser_client


def test_images_generations_with_tos_key(test_app):
    app, mock_client = test_app
    with patch.dict(app.extra if hasattr(app, "extra") else {}, {}):
        # Inject client into app closure
        from doubao2api import unified_server
        client = TestClient(app)
        
        # Test images/generations with direct ref_image_key
        resp = client.post(
            "/v1/images/generations",
            json={
                "prompt": "一只戴墨镜的猫",
                "ref_image_key": "tos-cn-i-mock/original.png",
                "size": "1024x1024",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["data"]) == 1
        assert data["data"][0]["url"] == "https://tos.mock.com/generated_img.png"
        mock_client.generate_image.assert_called_with(
            prompt="一只戴墨镜的猫",
            ratio="1:1",
            ref_image_key="tos-cn-i-mock/original.png",
        )


def test_images_generations_with_base64_data_uri(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    # 1x1 transparent png in base64
    b64_png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    resp = client.post(
        "/v1/images/generations",
        json={
            "prompt": "生成相似图片",
            "image": b64_png,
            "size": "1792x1024",
        },
    )
    assert resp.status_code == 200
    mock_client.upload_image.assert_called_once()
    mock_client.generate_image.assert_called_with(
        prompt="生成相似图片",
        ratio="16:9",
        ref_image_key="tos-cn-i-mock/ref_image_123.png",
    )


def test_images_edits_endpoint_json(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    resp = client.post(
        "/v1/images/edits",
        json={
            "prompt": "变成赛博朋克风格",
            "image": "tos-cn-i-mock/city.png",
            "size": "1024x1024",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["data"][0]["revised_prompt"] == "变成赛博朋克风格"
    mock_client.generate_image.assert_called_with(
        prompt="变成赛博朋克风格",
        ratio="1:1",
        ref_image_key="tos-cn-i-mock/city.png",
    )


def test_images_edits_endpoint_multipart(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    file_content = b"fake-png-data"
    resp = client.post(
        "/v1/images/edits",
        data={"prompt": "水彩画风格", "size": "1024x1024"},
        files={"image": ("test.png", file_content, "image/png")},
    )
    assert resp.status_code == 200
    mock_client.upload_image.assert_called_once()
    mock_client.generate_image.assert_called_with(
        prompt="水彩画风格",
        ratio="1:1",
        ref_image_key="tos-cn-i-mock/ref_image_123.png",
    )


def test_video_generations_with_reference_image(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    resp = client.post(
        "/v1/video/generations",
        json={
            "prompt": "让镜头向前推进，树叶随风飘落",
            "ratio": "16:9",
            "ref_image_key": "tos-cn-i-mock/autumn_forest.png",
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data"]) == 1
    assert data["data"][0]["video_url"] == "https://tos.mock.com/generated_vid.mp4"
    mock_client.generate_video.assert_called_with(
        prompt="让镜头向前推进，树叶随风飘落",
        ratio="16:9",
        ref_image_key="tos-cn-i-mock/autumn_forest.png",
    )


def test_ssrf_blocked_in_image_resolution(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    # Private IP should be blocked
    resp = client.post(
        "/v1/images/generations",
        json={
            "prompt": "测试内网拦截",
            "image": "http://127.0.0.1:8080/secret.png",
        },
    )
    assert resp.status_code == 400
    assert "SSRF" in resp.json()["error"]["message"]


def test_video_generations_with_multiple_tos_keys(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    resp = client.post(
        "/v1/video/generations",
        json={
            "prompt": "融合两张图片的风格与角色动作",
            "ratio": "16:9",
            "ref_image_keys": [
                "tos-cn-i-mock/character.png",
                "tos-cn-i-mock/background.png",
            ],
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["data"]) == 1
    mock_client.generate_video.assert_called_with(
        prompt="融合两张图片的风格与角色动作",
        ratio="16:9",
        ref_image_key="tos-cn-i-mock/character.png",
        ref_image_keys=[
            "tos-cn-i-mock/character.png",
            "tos-cn-i-mock/background.png",
        ],
    )


def test_video_generations_with_comma_separated_keys(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    resp = client.post(
        "/v1/video/generations",
        json={
            "prompt": "视频生成测试逗号分隔",
            "ratio": "16:9",
            "ref_image_keys": "tos-cn-i-mock/k1.png, tos-cn-i-mock/k2.png",
        },
    )
    assert resp.status_code == 200
    mock_client.generate_video.assert_called_with(
        prompt="视频生成测试逗号分隔",
        ratio="16:9",
        ref_image_key="tos-cn-i-mock/k1.png",
        ref_image_keys=[
            "tos-cn-i-mock/k1.png",
            "tos-cn-i-mock/k2.png",
        ],
    )


def test_video_generations_with_multiple_base64_images(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    b64_img = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    upload_responses = [
        {"uri": "tos-cn-i-mock/img_1.png"},
        {"uri": "tos-cn-i-mock/img_2.png"},
    ]
    mock_client.upload_image = AsyncMock(side_effect=upload_responses)

    resp = client.post(
        "/v1/video/generations",
        json={
            "prompt": "使用两个base64图片生成视频",
            "images": [b64_img, b64_img],
        },
    )
    assert resp.status_code == 200
    assert mock_client.upload_image.call_count == 2
    mock_client.generate_video.assert_called_with(
        prompt="使用两个base64图片生成视频",
        ratio=None,
        ref_image_key="tos-cn-i-mock/img_1.png",
        ref_image_keys=[
            "tos-cn-i-mock/img_1.png",
            "tos-cn-i-mock/img_2.png",
        ],
    )


def test_video_generations_multipart_multiple_files(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    upload_responses = [
        {"uri": "tos-cn-i-mock/file_1.png"},
        {"uri": "tos-cn-i-mock/file_2.png"},
    ]
    mock_client.upload_image = AsyncMock(side_effect=upload_responses)

    resp = client.post(
        "/v1/video/generations",
        data={"prompt": "多文件上传视频生成", "ratio": "9:16"},
        files=[
            ("images", ("frame1.png", b"image1-data", "image/png")),
            ("images", ("frame2.png", b"image2-data", "image/png")),
        ],
    )
    assert resp.status_code == 200
    assert mock_client.upload_image.call_count == 2
    mock_client.generate_video.assert_called_with(
        prompt="多文件上传视频生成",
        ratio="9:16",
        ref_image_key="tos-cn-i-mock/file_1.png",
        ref_image_keys=[
            "tos-cn-i-mock/file_1.png",
            "tos-cn-i-mock/file_2.png",
        ],
    )


def test_images_generations_with_multiple_keys(test_app):
    app, mock_client = test_app
    client = TestClient(app)

    resp = client.post(
        "/v1/images/generations",
        json={
            "prompt": "多图参考生图",
            "ref_image_keys": ["tos-cn-i-mock/r1.png", "tos-cn-i-mock/r2.png"],
            "size": "1024x1024",
        },
    )
    assert resp.status_code == 200
    mock_client.generate_image.assert_called_with(
        prompt="多图参考生图",
        ratio="1:1",
        ref_image_key="tos-cn-i-mock/r1.png",
        ref_image_keys=["tos-cn-i-mock/r1.png", "tos-cn-i-mock/r2.png"],
    )


def test_browser_client_generate_video_attachments_payload():
    import asyncio
    import json
    from doubao2api.browser_client import BrowserClient

    async def _run():
        client = BrowserClient(headless=True)
        captured = {}

        async def fake_chat_completion(text, image_attachments=None, use_deep_think=0, chat_ability=None, **kwargs):
            captured["text"] = text
            captured["image_attachments"] = image_attachments
            captured["chat_ability"] = chat_ability
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
                                                {"video": {"video_url": "https://test.mp4"}, "duration": 10}
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

        res = await client.generate_video(
            prompt="测试视频多图",
            ratio="16:9",
            ref_image_keys=["key_alpha", "key_beta"],
            model="seedance-2.0-fast",
            duration=10,
        )
        assert res["videos"][0]["video_url"] == "https://test.mp4"
        assert [a["uri"] for a in captured["image_attachments"]] == ["key_alpha", "key_beta"]
        assert captured["chat_ability"]["ability_type"] == 17
        ab = json.loads(captured["chat_ability"]["ability_param"])
        assert ab["model"] == "seedance_v2.0"
        assert ab["ratio"] == "16:9"
        assert ab["duration"] == 10

    asyncio.run(_run())


def test_browser_client_generate_image_attachments_payload():
    import asyncio
    from doubao2api.browser_client import BrowserClient

    async def _run():
        client = BrowserClient(headless=True)
        captured = {}

        async def fake_chat_completion(text, image_attachments=None, use_deep_think=0, **kwargs):
            captured["text"] = text
            captured["image_attachments"] = image_attachments
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
                                                        "key": "gen_key_1",
                                                        "image_ori": {
                                                            "url": "https://img.test/out.png",
                                                            "width": 1024,
                                                            "height": 1024,
                                                        },
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

        res = await client.generate_image(
            prompt="测试生图多图",
            ratio="1:1",
            ref_image_keys=["key_alpha", "key_beta"],
        )
        assert res["images"][0]["url"] == "https://img.test/out.png"
        assert [a["uri"] for a in captured["image_attachments"]] == ["key_alpha", "key_beta"]
        assert "1:1" in captured["text"]

    asyncio.run(_run())


def test_doubao_chat_client_generate_video_attachments_payload():
    import asyncio
    import json
    from unittest.mock import MagicMock
    from doubao2api.client import DoubaoChatClient

    async def _run():
        client = DoubaoChatClient(cookies={"sessionid": "mock_cookie"}, ms_token="dummy")
        captured_data = {}

        class DummyResponse:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            async def read(self):
                sse_event = {
                    "event_type": 2001,
                    "event_data": json.dumps({
                        "fin_reason": {"reason": 1, "async_task": {"id": "task_123"}}
                    }),
                }
                return f"data: {json.dumps(sse_event)}\n\n".encode("utf-8")

        calls = []
        def fake_post(url, data=None, headers=None, timeout=None):
            calls.append((url, json.loads(data) if data else {}))
            return DummyResponse()

        client._session = MagicMock()
        client._session.post = fake_post
        client._poll_video_result = AsyncMock(return_value=MagicMock())

        await client.generate_video(
            prompt="测试HTTP多图参考",
            ref_image_keys=["key1", "key2"],
        )
        assert len(calls) >= 1
        captured_data = calls[0][1]
        assert len(captured_data["messages"]) == 1
        msg = captured_data["messages"][0]
        assert msg["content_type"] == 2020
        assert msg["attachments"] == [
            {"type": "image", "key": "key1"},
            {"type": "image", "key": "key2"},
        ]

    asyncio.run(_run())


