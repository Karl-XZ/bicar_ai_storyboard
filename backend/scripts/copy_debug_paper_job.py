from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _job_dir() -> Path:
    path = ROOT / "local_storage" / "debug_paper_copy_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    safe_id = "".join(char for char in str(job_id) if char.isalnum() or char in {"_", "-"})
    return _job_dir() / f"{safe_id}.json"


def _write_job_state(job_id: str, state: dict) -> None:
    path = _job_path(job_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


async def _run(job_id: str, title: str) -> None:
    _write_job_state(job_id, {"status": "running", "title": title, "message": "正在加载飞书上传模块"})
    from app.core.config import settings
    from app.adapters.feishu import FeishuClient

    source_path = Path(settings.debug_paper_template_path).expanduser()
    if not source_path.exists() or not source_path.is_file():
        raise FileNotFoundError(f"模板文件不存在：{source_path}")
    target_folder = _folder_token_from_url(settings.debug_paper_target_folder_url) or settings.feishu_root_folder_token or "root"
    filename = _docx_filename(title)
    _write_job_state(job_id, {"status": "running", "title": title, "message": "正在读取模板文件"})
    content = source_path.read_bytes()
    _write_job_state(job_id, {"status": "running", "title": title, "message": "正在上传到飞书"})
    upload = await FeishuClient().upload_file(target_folder, filename, content)
    data = upload.get("data") or {}
    file_token = str(data.get("file_token") or data.get("file", {}).get("file_token") or "")
    url = _drive_file_url(file_token, settings.feishu_base_url) if file_token else _drive_folder_url(target_folder, settings.feishu_base_url)
    _write_job_state(
        job_id,
        {
            "status": "done",
            "title": title,
            "url": url,
            "folder_token": target_folder,
        },
    )


def _folder_token_from_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(str(url))
    parts = [part for part in parsed.path.split("/") if part]
    try:
        index = parts.index("folder")
        return parts[index + 1] if len(parts) > index + 1 else None
    except ValueError:
        return None


def _docx_filename(title: str) -> str:
    value = re.sub(r"\s+", " ", str(title or "").strip())
    value = re.sub(r'[\\/:*?"<>|]+', "_", value).strip(" .")
    if value.lower().endswith(".docx"):
        value = value[:-5].strip(" .")
    if not value:
        raise ValueError("文件名不能为空")
    return f"{value[:120]}.docx"


def _feishu_site_url(base_url: str) -> str:
    domain = base_url.replace("https://open.", "").replace("http://open.", "")
    return f"https://{domain}"


def _drive_file_url(file_token: str, base_url: str) -> str:
    return f"{_feishu_site_url(base_url)}/file/{file_token}"


def _drive_folder_url(folder_token: str, base_url: str) -> str:
    return f"{_feishu_site_url(base_url)}/drive/folder/{folder_token}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy debug paper template into Feishu.")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--title", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(_run(args.job_id, args.title), timeout=120))
        return 0
    except TimeoutError:
        _write_job_state(
            args.job_id,
            {
                "status": "failed",
                "title": args.title,
                "error": "TimeoutError: 飞书上传 120 秒内没有完成",
            },
        )
        return 1
    except Exception as exc:
        _write_job_state(
            args.job_id,
            {
                "status": "failed",
                "title": args.title,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        raise


if __name__ == "__main__":
    raise SystemExit(main())
