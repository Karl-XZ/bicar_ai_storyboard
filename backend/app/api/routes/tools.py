from __future__ import annotations

import io
import json
import shlex
import subprocess
import sys
from html import escape
from pathlib import Path
from urllib.parse import parse_qs
from uuid import uuid4

from fastapi import APIRouter, Request, status
from fastapi.responses import HTMLResponse, Response

from app.core.config import settings


router = APIRouter()


@router.get("/tools/debug-paper-copy", response_class=HTMLResponse, name="debug_paper_copy_form")
async def debug_paper_copy_form(request: Request) -> HTMLResponse:
    return HTMLResponse(_copy_form_html(action_url=_public_url("/tools/debug-paper-copy/create"), method="get", error=""))


@router.post("/tools/debug-paper-copy", response_class=HTMLResponse)
async def debug_paper_copy_submit(request: Request) -> HTMLResponse:
    form = parse_qs((await request.body()).decode("utf-8", errors="replace"))
    title = (form.get("name") or [""])[0].strip()
    return await _create_debug_paper_copy(title)


@router.get("/tools/debug-paper-copy/create", response_class=HTMLResponse)
async def debug_paper_copy_create(name: str = "") -> HTMLResponse:
    return await _create_debug_paper_copy(name.strip())


async def _create_debug_paper_copy(title: str) -> HTMLResponse:
    if not title:
        return HTMLResponse(
            _copy_form_html(action_url=_public_url("/tools/debug-paper-copy/create"), method="get", error="请先填写副本文件名。"),
            status_code=status.HTTP_400_BAD_REQUEST,
        )
    try:
        job_id = _start_debug_paper_job(title)
    except Exception as exc:
        return HTMLResponse(
            _copy_form_html(action_url=_public_url("/tools/debug-paper-copy/create"), method="get", error=f"启动创建任务失败：{type(exc).__name__}: {exc}"),
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return HTMLResponse(_copy_status_html(job_id=job_id, state=_read_job_state(job_id), just_started=True))


@router.get("/tools/debug-paper-copy/status/{job_id}", response_class=HTMLResponse)
async def debug_paper_copy_status(job_id: str) -> HTMLResponse:
    return HTMLResponse(_copy_status_html(job_id=job_id, state=_read_job_state(job_id), just_started=False))


def _start_debug_paper_job(title: str) -> str:
    job_id = uuid4().hex
    _write_job_state(job_id, {"status": "running", "title": title, "message": "正在复制并上传到飞书"})
    backend_dir = Path(__file__).resolve().parents[3]
    script_path = backend_dir / "scripts" / "copy_debug_paper_job.py"
    python_executable = backend_dir / ".venv311" / "bin" / "python"
    executable = str(python_executable if python_executable.exists() else sys.executable)
    logs_dir = backend_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = open(logs_dir / f"debug_paper_copy_{job_id}.log", "ab", buffering=0)
    command = " ".join(
        [
            "cd",
            shlex.quote(str(backend_dir)),
            "&&",
            "exec",
            shlex.quote(executable),
            shlex.quote(str(script_path)),
            "--job-id",
            shlex.quote(job_id),
            "--title",
            shlex.quote(title),
        ]
    )
    subprocess.Popen(
        ["/bin/zsh", "-lc", command],
        cwd=str(backend_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        close_fds=True,
        start_new_session=True,
    )
    return job_id


@router.get("/tools/debug-paper-copy/qr", response_class=HTMLResponse)
async def debug_paper_copy_qr_page() -> HTMLResponse:
    form_url = _public_url("/tools/debug-paper-copy")
    qr_url = _public_url("/tools/debug-paper-copy/qr.svg")
    return HTMLResponse(
        "\n".join(
            [
                "<!doctype html>",
                '<html lang="zh-CN">',
                "<head>",
                '<meta charset="utf-8">',
                '<meta name="viewport" content="width=device-width, initial-scale=1">',
                "<title>调试纸复制二维码</title>",
                _page_style(),
                "</head>",
                "<body>",
                '<main class="card">',
                "<h1>调试纸复制二维码</h1>",
                "<p>扫码后输入文件名，系统会复制一份调试纸文档并上传到飞书。</p>",
                f'<img class="qr" src="{escape(qr_url)}" alt="调试纸复制二维码">',
                f'<p class="link">无法扫码时打开：<a href="{escape(form_url)}">{escape(form_url)}</a></p>',
                "</main>",
                "</body>",
                "</html>",
            ]
        )
    )


@router.get("/tools/debug-paper-copy/qr.svg")
async def debug_paper_copy_qr_svg() -> Response:
    form_url = _public_url("/tools/debug-paper-copy")
    try:
        import qrcode
        import qrcode.image.svg
    except ModuleNotFoundError:
        svg = _fallback_svg("qrcode dependency missing", form_url)
        return Response(svg, media_type="image/svg+xml")
    image = qrcode.make(form_url, image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(buffer.getvalue(), media_type="image/svg+xml")


def _public_url(path: str) -> str:
    base = str(settings.app_public_base_url or "http://127.0.0.1:8000").rstrip("/")
    return f"{base}{path}"


def _copy_form_html(*, action_url: str, method: str, error: str) -> str:
    error_block = f'<p class="error">{escape(error)}</p>' if error else ""
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>创建调试纸副本</title>",
            _page_style(),
            "</head>",
            "<body>",
            '<main class="card">',
            "<h1>创建调试纸副本</h1>",
            "<p>请输入这份副本文档在飞书里的名称。提交后会复制上传一份新的 <code>.docx</code> 文件。</p>",
            error_block,
            f'<form method="{escape(method)}" action="{escape(action_url)}">',
            '<label for="name">副本文件名</label>',
            '<input id="name" name="name" type="text" placeholder="例如：调试纸-张三-20260622" autofocus required>',
            '<button type="submit">创建副本</button>',
            "</form>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _copy_success_html(*, title: str, file_url: str, folder_token: str) -> str:
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            "<title>调试纸副本已创建</title>",
            _page_style(),
            "</head>",
            "<body>",
            '<main class="card">',
            "<h1>副本已创建</h1>",
            f"<p>文件名：<strong>{escape(title)}.docx</strong></p>",
            f'<p>副本位置：<a href="{escape(file_url)}">{escape(file_url)}</a></p>',
            f"<p class=\"muted\">目标文件夹 token：{escape(folder_token)}</p>",
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _copy_status_html(*, job_id: str, state: dict, just_started: bool) -> str:
    status_text = str(state.get("status") or "missing")
    title = str(state.get("title") or "")
    status_url = _public_url(f"/tools/debug-paper-copy/status/{job_id}")
    if status_text == "done":
        return _copy_success_html(
            title=title,
            file_url=str(state.get("url") or ""),
            folder_token=str(state.get("folder_token") or ""),
        )
    if status_text == "failed":
        error = str(state.get("error") or "未知错误")
        return _copy_form_html(
            action_url=_public_url("/tools/debug-paper-copy/create"),
            method="get",
            error=f"创建副本失败：{error}",
        )
    refresh = f'<meta http-equiv="refresh" content="2; url={escape(status_url)}">'
    started = "任务已开始。" if just_started else "仍在创建中。"
    return "\n".join(
        [
            "<!doctype html>",
            '<html lang="zh-CN">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            refresh,
            "<title>正在创建调试纸副本</title>",
            _page_style(),
            "</head>",
            "<body>",
            '<main class="card">',
            "<h1>正在创建副本</h1>",
            f"<p>{escape(started)}页面会自动刷新，完成后显示飞书链接。</p>",
            f"<p>文件名：<strong>{escape(title)}.docx</strong></p>",
            f'<p class="link">状态页：<a href="{escape(status_url)}">{escape(status_url)}</a></p>',
            "</main>",
            "</body>",
            "</html>",
        ]
    )


def _job_dir() -> Path:
    path = Path(settings.storage_local_root) / "debug_paper_copy_jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_path(job_id: str) -> Path:
    safe_id = "".join(char for char in str(job_id) if char.isalnum() or char in {"_", "-"})
    return _job_dir() / f"{safe_id}.json"


def _read_job_state(job_id: str) -> dict:
    path = _job_path(job_id)
    if not path.exists():
        return {"status": "missing", "title": "", "error": "任务不存在或服务已重启"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "failed", "title": "", "error": f"读取任务状态失败：{type(exc).__name__}: {exc}"}


def _write_job_state(job_id: str, state: dict) -> None:
    path = _job_path(job_id)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _page_style() -> str:
    return """
<style>
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f4efe7; color: #1d1d1f; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.card { width: min(92vw, 520px); padding: 28px; border-radius: 24px; background: #fffaf1; box-shadow: 0 18px 50px rgba(61, 43, 20, .16); border: 1px solid rgba(94, 67, 31, .12); }
h1 { margin: 0 0 14px; font-size: 26px; }
p { line-height: 1.7; }
label { display: block; margin: 22px 0 8px; font-weight: 700; }
input { width: 100%; box-sizing: border-box; padding: 14px 16px; border-radius: 14px; border: 1px solid #d6c6ad; font-size: 16px; background: white; }
button { margin-top: 18px; width: 100%; border: 0; border-radius: 14px; padding: 14px 18px; background: #1b4332; color: white; font-size: 16px; font-weight: 700; }
a { color: #0f5ec7; word-break: break-all; }
.qr { display: block; width: min(70vw, 280px); margin: 22px auto; background: white; border-radius: 18px; padding: 16px; }
.error { padding: 12px 14px; border-radius: 12px; background: #ffe4df; color: #9f1d12; }
.muted { color: #70685d; font-size: 13px; }
.link { font-size: 14px; }
</style>
"""


def _fallback_svg(message: str, url: str) -> str:
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="520" height="180" viewBox="0 0 520 180">
<rect width="520" height="180" fill="#fffaf1"/>
<text x="24" y="58" font-size="20" fill="#1d1d1f">{escape(message)}</text>
<text x="24" y="104" font-size="14" fill="#0f5ec7">{escape(url)}</text>
</svg>"""
