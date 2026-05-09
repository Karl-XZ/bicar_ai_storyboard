import asyncio
import json

import httpx

from app.core.config import settings
from app.providers.xyq_nest import XYQNestVideoProvider


class FakeAsyncClient:
    def __init__(self, *, timeout=None, follow_redirects=False):
        self.timeout = timeout
        self.follow_redirects = follow_redirects

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, url, headers=None):
        if url == "https://download.example/video.mp4":
            return httpx.Response(
                200,
                headers={"content-type": "video/mp4"},
                content=b"video-bytes",
                request=httpx.Request("GET", "https://download.example/video.mp4"),
            )
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=b"png-bytes",
            request=httpx.Request("GET", url),
        )

    async def post(self, url, headers=None, json=None, data=None, files=None):
        if url.endswith("/upload_file"):
            filename = files["file"][0]
            return httpx.Response(
                200,
                json={"ret": "0", "errmsg": "", "data": {"pippit_asset_id": f"asset_for_{filename}"}},
                request=httpx.Request("POST", url),
            )
        if url.endswith("/submit_run"):
            return httpx.Response(
                200,
                json={
                    "ret": "0",
                    "errmsg": "",
                    "data": {
                        "run": {"thread_id": "thread-123", "run_id": "run-456"},
                        "web_thread_link": "https://xyq.jianying.com/thread-123",
                    },
                },
                request=httpx.Request("POST", url),
            )
        if url.endswith("/get_thread"):
            return httpx.Response(
                200,
                json={
                    "ret": "0",
                    "errmsg": "",
                    "data": {
                        "thread": {
                            "run_list": [
                                {
                                    "state": 3,
                                    "entry_list": [
                                        {
                                            "artifact": {
                                                "artifact_id": "artifact-1",
                                                "role": "assistant",
                                                "content": [
                                                    {
                                                        "type": "video",
                                                        "data": {"url": "https://download.example/video.mp4"},
                                                    }
                                                ],
                                            }
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                },
                request=httpx.Request("POST", url),
            )
        raise AssertionError(f"Unexpected URL: {url}")


class FakeAsyncClientWithStringifiedArtifact(FakeAsyncClient):
    async def post(self, url, headers=None, json=None, data=None, files=None):
        if url.endswith("/get_thread"):
            return httpx.Response(
                200,
                json={
                    "ret": "0",
                    "errmsg": "",
                    "data": {
                        "thread": {
                            "run_list": [
                                {
                                    "state": 3,
                                    "entry_list": [
                                        {
                                            "type": 2,
                                            "artifact": {
                                                "artifact_id": "artifact-1",
                                                "content": [
                                                    {
                                                        "type": "data",
                                                        "sub_type": "biz/x_data_video",
                                                        "data": (
                                                            '{"video":{"url":"https://download.example/video.mp4",'
                                                            '"metadata":{"ratio":"16:9"}}}'
                                                        ),
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            ]
                        }
                    },
                },
                request=httpx.Request("POST", url),
            )
        return await super().post(url, headers=headers, json=json, data=data, files=files)


def test_xyq_provider_submits_with_uploaded_assets(monkeypatch, tmp_path):
    first_frame = tmp_path / "first.png"
    last_frame = tmp_path / "last.png"
    first_frame.write_bytes(b"first-bytes")
    last_frame.write_bytes(b"last-bytes")

    monkeypatch.setattr(settings, "xyq_access_key", "test-xyq-key")
    monkeypatch.setattr(settings, "xyq_base_url", "https://xyq.jianying.com")
    monkeypatch.setattr("app.providers.xyq_nest.httpx.AsyncClient", FakeAsyncClient)

    task = asyncio.run(
        XYQNestVideoProvider().create_video_task(
            {
                "prompt": "一个追车镜头，节奏紧凑",
                "camera_motion": "快速跟拍",
                "duration_seconds": 5,
                "first_frame_url": f"file://{first_frame}",
                "last_frame_url": f"file://{last_frame}",
            }
        )
    )

    payload = json.loads(task.provider_task_id)
    assert payload["thread_id"] == "thread-123"
    assert payload["run_id"] == "run-456"


def test_xyq_provider_uploads_feishu_reference_asset(monkeypatch):
    async def fake_download_drive_file(self, file_token):
        assert file_token == "file_tok_123"
        return ("reference.png", b"feishu-image-bytes", "image/png")

    monkeypatch.setattr(settings, "xyq_access_key", "test-xyq-key")
    monkeypatch.setattr(settings, "xyq_base_url", "https://xyq.jianying.com")
    monkeypatch.setattr("app.providers.xyq_nest.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr("app.providers.xyq_nest.FeishuClient.download_drive_file", fake_download_drive_file)

    task = asyncio.run(
        XYQNestVideoProvider().create_video_task(
            {
                "prompt": "参考图驱动的短视频",
                "reference_image_url": "feishu://file_tok_123",
            }
        )
    )

    payload = json.loads(task.provider_task_id)
    assert payload["thread_id"] == "thread-123"
    assert payload["run_id"] == "run-456"


def test_xyq_provider_normalizes_human_reference_policy_error(monkeypatch):
    monkeypatch.setattr(settings, "xyq_access_key", "test-xyq-key")
    monkeypatch.setattr(settings, "xyq_base_url", "https://xyq.jianying.com")
    monkeypatch.setattr(settings, "video_max_polling_attempts", 1)

    class FakeAsyncClientWithPolicyFailure(FakeAsyncClient):
        async def post(self, url, headers=None, json=None, data=None, files=None):
            if url.endswith("/get_thread"):
                return httpx.Response(
                    200,
                    json={
                        "ret": "0",
                        "errmsg": "",
                        "data": {
                            "thread": {
                                "run_list": [
                                    {
                                        "state": 4,
                                        "fail_reason": "reference human face image is not allowed by policy",
                                    }
                                ]
                            }
                        },
                    },
                    request=httpx.Request("POST", url),
                )
            return await super().post(url, headers=headers, json=json, data=data, files=files)

    monkeypatch.setattr("app.providers.xyq_nest.httpx.AsyncClient", FakeAsyncClientWithPolicyFailure)

    try:
        asyncio.run(
            XYQNestVideoProvider().poll_video_task(
                json.dumps({"thread_id": "thread-123", "run_id": "run-456"}, separators=(",", ":"))
            )
        )
    except Exception as exc:
        assert str(exc) == "小云雀 当前不支持上传真人参考图。请改用非真人参考图，或切换到其他视频模型后重试。"
    else:
        raise AssertionError("expected 小云雀 human-image policy failure")


def test_xyq_provider_polls_and_downloads_video(monkeypatch):
    monkeypatch.setattr(settings, "xyq_access_key", "test-xyq-key")
    monkeypatch.setattr(settings, "xyq_base_url", "https://xyq.jianying.com")
    monkeypatch.setattr(settings, "video_max_polling_attempts", 1)
    monkeypatch.setattr("app.providers.xyq_nest.httpx.AsyncClient", FakeAsyncClient)

    result = asyncio.run(
        XYQNestVideoProvider().poll_video_task(
            json.dumps({"thread_id": "thread-123", "run_id": "run-456"}, separators=(",", ":"))
        )
    )

    assert result["status"] == "succeeded"
    assert result["video_bytes"] == b"video-bytes"
    assert result["mime_type"] == "video/mp4"


def test_xyq_provider_polls_stringified_artifact_payload(monkeypatch):
    monkeypatch.setattr(settings, "xyq_access_key", "test-xyq-key")
    monkeypatch.setattr(settings, "xyq_base_url", "https://xyq.jianying.com")
    monkeypatch.setattr(settings, "video_max_polling_attempts", 1)
    monkeypatch.setattr("app.providers.xyq_nest.httpx.AsyncClient", FakeAsyncClientWithStringifiedArtifact)

    result = asyncio.run(
        XYQNestVideoProvider().poll_video_task(
            json.dumps({"thread_id": "thread-123", "run_id": "run-456"}, separators=(",", ":"))
        )
    )

    assert result["status"] == "succeeded"
    assert result["video_bytes"] == b"video-bytes"
    assert result["mime_type"] == "video/mp4"
