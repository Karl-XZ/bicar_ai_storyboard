# Biche Storyboard Backend

飞书 AI 分镜到视频生成工作流后端。核心入口是飞书机器人事件、多维表格触发器和项目/镜头 API；本地未配置真实模型密钥时会使用 mock provider 完成全流程验收。

## Local services

```powershell
docker compose up -d postgres redis minio
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

本地无需 Postgres 时可临时使用 SQLite：

```powershell
$env:DATABASE_URL='sqlite+pysqlite:///./local_storage/dev_server.db'
python scripts\init_db.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

飞书真实权限验收：

```powershell
python scripts\check_feishu_auth.py
python scripts\feishu_acceptance_check.py
python scripts\check_dashscope.py
```

飞书后台如果选择的是“使用长连接接收事件”，还需要额外运行长连接进程：

```powershell
python scripts\run_feishu_ws.py
```

这个进程负责接收 `im.message.receive_v1` 消息事件、`drive.file.bitable_record_changed_v1` 多维表格记录变更事件和卡片按钮回调。它必须常驻运行；停止后，群里的 `@机器人 help`、`@机器人 新建分镜项目：...`、表格新增/编辑自动补默认值和卡片按钮都不会触发后端。

飞书开发者后台还需要订阅事件：

- `im.message.receive_v1`：群聊/私聊文字命令
- `drive.file.bitable_record_changed_v1`：多维表格新增/编辑记录
- `card.action.trigger`：项目卡片按钮

如果只收到文字命令，点击卡片按钮提示 `code: 200340`，通常是 `card.action.trigger` 未订阅或机器人消息卡片交互能力未启用。

卡片按钮不可用时，可以在群里直接发送这些文字命令：

- `优化当前批次 Prompt`
- `生成全部图片`
- `生成全部视频`
- `生成全部图片和视频`
- `启动首尾帧同步`
- `启动关键帧生成`
- `同步表格`
- `查看进度`

新建项目会自动创建 3 行模板；同步、优化、生成前也会补齐缺失的 `镜号`、`生成批次`、`审核状态`、`生成状态`、`Prompt 版本` 和默认模型字段。`审核状态` 只记录人工审核结果；单条视频生成由 `生成状态=启动` 触发。

默认只生成首帧和尾帧。需要候选关键帧时，先点击卡片里的 `启动关键帧生成` 或发送同名命令。开启 `启动首尾帧同步` 后，后一镜头首帧会复用上一镜头尾帧，表格中首帧/尾帧只展示最新一张，避免重复附件；图片也会同步上传到项目的 `02_帧图` 文件夹。

多维表格记录变更属于云文档事件。除了在开发者后台订阅 `drive.file.bitable_record_changed_v1`，每份新建的 base 还会自动调用 `/drive/v1/files/{file_token}/subscribe?file_type=bitable` 完成文件级订阅；否则编辑表格不会推送到长连接。

## Model defaults

默认生成链路使用 DashScope：

- 文本 Prompt 优化：`qwen-plus`
- 图片生成：`wanx2.1-t2i-turbo`
- 视频生成：`wan2.2-kf2v-flash`

对应环境变量：

```powershell
DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/api/v1
DASHSCOPE_COMPATIBLE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
DASHSCOPE_TEXT_MODEL=qwen-plus
DASHSCOPE_IMAGE_MODEL=wanx2.1-t2i-turbo
DASHSCOPE_VIDEO_MODEL=wan2.2-kf2v-flash
DEFAULT_TEXT_PROVIDER=dashscope
DEFAULT_IMAGE_PROVIDER=dashscope
DEFAULT_VIDEO_PROVIDER=dashscope
```

飞书分镜表中也会生成 `文本模型`、`图片模型`、`视频模型` 三列。单行填写这些字段时，会覆盖项目默认模型。

## Queues

- `prompt.optimize`
- `image.generate`
- `video.generate`
- `provider.polling`
- `feishu.backfill`
- `archive`

## Required secrets

`docker-compose.yml` reads the private `.env` file. Keep `.env.example` as the variable template and never commit `.env`.
