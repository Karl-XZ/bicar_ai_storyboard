from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.video_downloads import VideoDownloadService


async def _run(*, apply: bool, document_urls: tuple[str, ...], cleanup_legacy_empty_folders: bool) -> None:
    service = VideoDownloadService()
    report = await service.organize_existing_downloads(
        dry_run=not apply,
        document_urls=document_urls,
        cleanup_legacy_empty_folders=cleanup_legacy_empty_folders,
    )
    payload = asdict(report)
    payload["mode"] = "apply" if apply else "dry_run"
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="整理飞书 AI生成/视频下载 中已有视频到文档子文件夹或非文档视频下载。")
    parser.add_argument("--apply", action="store_true", help="实际移动文件并回填表格；不加时只预览。")
    parser.add_argument(
        "--document-url",
        action="append",
        default=[],
        help="用于历史整理的飞书文档链接；脚本会读取文档真实链接集合，并按链接/video_id 归属到该文档原名文件夹。",
    )
    parser.add_argument("--cleanup-legacy-empty-folders", action="store_true", help="清理空的旧式 文档_<token> 文件夹。")
    args = parser.parse_args()
    asyncio.run(
        _run(
            apply=args.apply,
            document_urls=tuple(args.document_url or ()),
            cleanup_legacy_empty_folders=args.cleanup_legacy_empty_folders,
        )
    )


if __name__ == "__main__":
    main()
