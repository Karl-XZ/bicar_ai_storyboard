from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app.core.config import settings  # noqa: E402


async def main() -> int:
    if not settings.dashscope_api_key:
        print(json.dumps({"ok": False, "error": "DASHSCOPE_API_KEY is missing"}, ensure_ascii=False))
        return 2
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.dashscope_compatible_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.dashscope_api_key}", "Content-Type": "application/json"},
            json={
                "model": settings.dashscope_text_model,
                "messages": [{"role": "user", "content": "只输出 OK"}],
                "max_tokens": 8,
            },
        )
    try:
        data = response.json()
    except ValueError:
        print(json.dumps({"ok": False, "status": response.status_code, "error": response.text[:300]}, ensure_ascii=False))
        return 1
    if response.status_code >= 400 or data.get("code"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": response.status_code,
                    "code": data.get("code"),
                    "message": data.get("message"),
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps({"ok": True, "model": settings.dashscope_text_model}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
