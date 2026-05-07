from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.feishu import FeishuApiError, FeishuClient
from app.adapters.feishu_auth import FeishuAuthClient
from app.core.config import settings


SCOPE_PATTERN = re.compile(r"\[([a-z0-9_:,.\-\s]+)\]")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Check Feishu permissions required by the storyboard user flow.")
    parser.add_argument("--chat-id", default=settings.feishu_default_chat_id)
    parser.add_argument("--probe-name", default=f"acceptance_probe_{int(time.time())}")
    args = parser.parse_args()

    result: dict[str, Any] = {"ok": True, "checks": []}
    auth = FeishuAuthClient()
    client = FeishuClient(auth=auth)

    try:
        token = await auth.get_tenant_access_token()
        result["checks"].append({"name": "tenant_access_token", "ok": True, "token_prefix": token[:8]})
    except Exception as exc:  # noqa: BLE001
        result["ok"] = False
        result["checks"].append({"name": "tenant_access_token", "ok": False, "error": str(exc)})
        print(json.dumps(result, ensure_ascii=False))
        return

    await _check(
        result,
        "send_bot_message",
        client.send_text(args.chat_id, "飞书 AI 分镜验收：机器人消息权限检查。"),
    )
    await _check(result, "create_folder", client.create_folder(settings.feishu_root_folder_token or "root", args.probe_name))
    await _check(
        result,
        "create_bitable_app",
        client.create_bitable_app(f"{args.probe_name}_bitable", folder_token=settings.feishu_root_folder_token),
    )

    result["ok"] = all(item.get("ok") for item in result["checks"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


async def _check(result: dict[str, Any], name: str, coro) -> None:
    try:
        response = await coro
        data = response.get("data", {})
        result["checks"].append(
            {
                "name": name,
                "ok": True,
                "data_keys": sorted(data.keys()),
                "token_prefix": str(data.get("token") or data.get("app_token") or data.get("message_id") or "")[:8],
            }
        )
    except FeishuApiError as exc:
        result["checks"].append(
            {
                "name": name,
                "ok": False,
                "error": str(exc),
                "required_scopes": _required_scopes(exc.body),
            }
        )
    except Exception as exc:  # noqa: BLE001
        result["checks"].append({"name": name, "ok": False, "error": str(exc)})


def _required_scopes(body: dict[str, Any]) -> list[str]:
    error = body.get("error") or {}
    violations = error.get("permission_violations") or []
    scopes = [item.get("subject") for item in violations if item.get("subject")]
    if scopes:
        return scopes
    msg = body.get("msg") or ""
    match = SCOPE_PATTERN.search(msg)
    return [item.strip() for item in match.group(1).split(",")] if match else []


if __name__ == "__main__":
    asyncio.run(main())
