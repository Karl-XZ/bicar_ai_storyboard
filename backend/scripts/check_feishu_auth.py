import json
import os
import sys
import urllib.request
from pathlib import Path


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def main() -> int:
    env_path = Path(__file__).resolve().parents[1] / ".env"
    load_env(env_path)

    app_id = os.environ.get("FEISHU_APP_ID", "")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    base_url = os.environ.get("FEISHU_BASE_URL", "https://open.feishu.cn").rstrip("/")
    if not app_id or not app_secret:
        print("missing FEISHU_APP_ID or FEISHU_APP_SECRET", file=sys.stderr)
        return 2

    request = urllib.request.Request(
        f"{base_url}/open-apis/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))

    if body.get("code") != 0:
        print(json.dumps({"ok": False, "code": body.get("code"), "msg": body.get("msg")}, ensure_ascii=False))
        return 1

    token = body.get("tenant_access_token", "")
    print(json.dumps({"ok": True, "expire": body.get("expire"), "token_prefix": token[:8]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

