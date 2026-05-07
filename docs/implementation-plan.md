# 飞书 AI 分镜真实实现计划

## 当前结论

现有 `biche-ui` 是 UI 原型，只能作为视觉风格参考。真实产品的主入口应是飞书自建应用 Chatbot、消息卡片和多维表格；后端负责异步任务编排、模型调用、资产存储、状态机、回填和归档。

## 目标闭环

1. 用户在飞书机器人中新建项目。
2. 后端创建项目记录、飞书项目目录、分镜多维表格，并返回项目卡片。
3. 编导在多维表格填写镜号、场景描述、参考图、Prompt、批次等字段。
4. 用户触发 Prompt 优化，后端调用文本模型输出结构化 JSON，并回填关键帧、首帧、尾帧、视频 Prompt、负面 Prompt。
5. 用户触发批量出图，后端并行生成首帧、尾帧和多个关键帧候选，并回填表格。
6. 审核人通过或驳回。通过前必须校验首帧、尾帧、选中关键帧、视频 Prompt 和 Prompt 版本。
7. 审核通过后，后端调用视频模型创建异步视频任务，轮询完成后下载、存储、回填视频链接。
8. 用户标记满意度，后端按满意/不满意归档并回填归档链接。

## 工程结构

```text
backend/
  app/
    api/routes/          FastAPI HTTP 接口和回调入口
    adapters/            飞书、对象存储等外部系统适配
    core/                配置、日志、安全基础设施
    domain/              枚举、DTO、状态语义
    models/              SQLAlchemy ORM
    providers/           Text/Image/Video Provider 接口和实现
    services/            业务服务：项目、镜头、Prompt、帧、视频、归档
    workers/             Celery 队列和任务入口
  tests/
biche-ui/biche-ui/       当前 UI 原型，后续改造成真实 API 驱动的管理/审核面板
```

## 首版后端边界

必须先实现 P0：

- FastAPI 项目、健康检查、配置、日志。
- Docker Compose：API、Postgres、Redis、Worker、MinIO。
- Alembic + SQLAlchemy：projects、shots、generation_jobs、assets、audit_logs、cost_logs。
- 飞书鉴权、回调签名校验、消息卡片、多维表格读写、云空间/文件夹。
- ProjectService：新建项目、复制/创建表格、初始化目录、失败回滚。
- 字段 ID 缓存，避免硬编码飞书字段名。

P1 进入真实闭环：

- Prompt 优化：结构化 JSON Schema，不能解析自然语言。
- 图片生成：Nano Banana 2 默认、GPT Image 2 fallback，任务幂等和部分失败恢复。
- 审核状态机：非法状态拒绝，驳回必须记录原因。
- Seedance 2.0 视频任务：创建、轮询、下载、存储、回填、重试。
- 满意度归档：对象存储为主，飞书目录为可选镜像。
- 飞书卡片 JSON：项目总览、批次操作、审核提醒、失败提醒。

P2 增强：

- 前后帧自动对齐。
- 成本统计和飞书回填。
- Prometheus/Grafana。
- DLQ 和手动恢复工具。
- 项目成员权限校验。

## 状态机原则

核心状态应使用后端枚举，不依赖飞书显示文本：

- `draft`
- `pending_prompt`
- `prompt_optimizing`
- `pending_frames`
- `frames_generating`
- `frame_partial_failed`
- `pending_review`
- `approved`
- `rejected`
- `video_queued`
- `video_generating`
- `video_failed`
- `pending_acceptance`
- `archived_satisfied`
- `archived_unsatisfied`

关键约束：

- 旧 `prompt_version` 的任务结果只能标记 `stale`，不能覆盖新结果。
- 同一镜头同一版本同一 Provider 的生成任务必须有幂等键。
- 已验收或已归档视频不能被失败重试覆盖。
- 飞书回填失败进入独立回填队列，不让模型任务重复执行。

## 接口契约

首批接口按文档固定：

- `POST /webhooks/feishu/events`
- `POST /webhooks/feishu/card-actions`
- `POST /webhooks/feishu/bitable-trigger`
- `POST /api/projects`
- `POST /api/projects/{project_id}/sync`
- `POST /api/projects/{project_id}/generate-current-batch`
- `POST /api/shots/{shot_id}/optimize-prompt`
- `POST /api/shots/{shot_id}/generate-frames`
- `POST /api/shots/{shot_id}/generate-video`
- `POST /api/shots/{shot_id}/archive`
- `GET /api/jobs/{job_id}`
- `POST /api/jobs/{job_id}/retry`
- `POST /webhooks/providers/seedance`

统一成功响应：

```json
{
  "success": true,
  "request_id": "req_xxx",
  "message": "任务已创建",
  "data": {}
}
```

统一错误响应：

```json
{
  "success": false,
  "request_id": "req_xxx",
  "error_code": "PROMPT_VERSION_CONFLICT",
  "message": "Prompt 已修改，请重新生成帧图后再生成视频"
}
```

## 真实实现前置依赖

需要从用户或部署环境提供：

- 飞书 App ID、App Secret、Encrypt Key、Verification Token、回调地址。
- 多维表格模板或创建模板的规则。
- OpenAI API Key。
- Google Gemini / Nano Banana 2 API Key。
- Seedance 2.0 API Key、模型 ID、回调签名规则。
- S3/OSS/COS/MinIO 存储配置。
- 生产域名或内网穿透地址，用于飞书回调和资源预览。

