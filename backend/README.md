# 哔车 AI 助手 / AI 分镜后端使用手册

本文档面向两类人：

- **使用者**：在飞书里和「哔车 AI 助手」对话、创建分镜项目、生成图片/视频、做 Deep Research、下载视频、复制调试纸文档。
- **维护者**：部署后端、配置飞书事件、接入模型、排查生成失败、运行测试和上线。

项目核心能力是一套围绕飞书的 AI 创作工作流：

- 飞书机器人普通聊天、Agent、Deep Research、分镜助手、分镜拆解多模式会话。
- 从飞书聊天命令创建「AI 分镜」项目，自动创建飞书文件夹和多维表格。
- 在飞书多维表格里维护镜头、Prompt、参考图、首尾帧、关键帧、视频状态和错误日志。
- 调用文本、图片、视频模型完成 Prompt 优化、首帧/尾帧/关键帧生成、视频生成。
- 支持 OpenRouter / DeepSeek / OpenAI / DashScope / Google Gemini / 小云雀 / Seedance 等模型接入。
- 支持视频下载工作流，把 YouTube/外部视频下载到飞书「AI生成/视频下载」并登记状态。
- 支持「视频拆分镜」：下载视频、抽帧、视觉模型分析、生成可编辑分镜表。
- 支持「调试纸二维码」：飞书原生表单提交名称后自动复制 `调试纸CN.docx` 并回填链接。

> 注意：本文不包含任何真实 API Key。`.env` 是私密配置，禁止提交到 Git。

---

## 演示截图

下面是一组从飞书移动端截取的完整流程示例：从 Agent 能力说明、文档/分镜需求发起，到项目创建、分镜表编辑、图片/视频生成与进度反馈。

<table>
  <tr>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/01-agent-capabilities.jpg" alt="Agent 能力说明" width="100%">
      <br>
      <sub>Agent 能力说明</sub>
    </td>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/02-doc-to-storyboard-request.jpg" alt="基于文档发起分镜制作需求" width="100%">
      <br>
      <sub>基于文档发起分镜制作需求</sub>
    </td>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/03-project-created-card.jpg" alt="分镜项目创建成功卡片" width="100%">
      <br>
      <sub>分镜项目创建成功卡片</sub>
    </td>
  </tr>
  <tr>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/04-storyboard-table-list.jpg" alt="飞书多维表分镜列表" width="100%">
      <br>
      <sub>飞书多维表分镜列表</sub>
    </td>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/05-shot-prompt-detail.jpg" alt="单镜头描述与 Prompt 字段" width="100%">
      <br>
      <sub>单镜头描述与 Prompt 字段</sub>
    </td>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/06-generation-settings.jpg" alt="模型、首尾帧与生成设置" width="100%">
      <br>
      <sub>模型、首尾帧与生成设置</sub>
    </td>
  </tr>
  <tr>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/07-video-result-status.jpg" alt="视频生成结果与状态" width="100%">
      <br>
      <sub>视频生成结果与状态</sub>
    </td>
    <td width="33%" align="center">
      <img src="../docs/assets/readme-demo/08-agent-progress-card.jpg" alt="Agent 生成进度反馈" width="100%">
      <br>
      <sub>Agent 生成进度反馈</sub>
    </td>
    <td width="33%" align="center">
      <em>更多示例可继续补充</em>
    </td>
  </tr>
</table>

---

## 1. 项目结构

常见目录如下：

```text
backend/
  app/
    adapters/          # 飞书 API、卡片、字段定义、签名等适配层
    api/routes/        # FastAPI HTTP 路由、飞书 webhook、工具页
    core/              # 配置、日志、模型别名
    db/                # SQLAlchemy session
    domain/            # 枚举和 Pydantic schema
    models/            # 数据库模型：项目、镜头、资产、任务、聊天偏好等
    providers/         # 模型 Provider：DeepSeek、OpenRouter、DashScope、小云雀等
    services/          # 核心业务：机器人命令、分镜、工作流、视频下载、Deep Research
    workers/           # Celery 任务
  alembic/             # 数据库迁移
  scripts/             # 本地运维、验收、部署、调试脚本
  tests/               # 单元测试和流程测试
  local_storage/       # 本地运行生成的缓存/文件/工作区元数据
```

关键文件：

- `app/services/bot_commands.py`：飞书机器人文字命令、模式切换、普通聊天、Agent、Deep Research、直接生成等入口。
- `app/services/feishu_storyboard.py`：分镜项目创建、表格同步、图片/视频生成、进度回填。
- `app/services/workflow.py`：Prompt 优化、图片生成、视频生成的模型调用编排。
- `app/services/video_downloads.py`：视频下载多维表、下载、上传、重试、整理。
- `app/services/video_storyboard.py`：视频拆分镜：下载视频、抽帧、视觉模型分析、写入分镜表。
- `app/services/debug_paper_form.py`：调试纸飞书原生表单、复制 `.docx`、权限设置。
- `app/adapters/feishu_fields.py`：分镜表字段定义。
- `app/core/model_aliases.py`：图片/视频模型别名，例如 `nanobanana`、`gpt2`、`小云雀`。

---

## 2. 本地启动

### 2.1 安装依赖

```bash
cd backend
python3 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt
```

### 2.2 准备 `.env`

从模板复制：

```bash
cp .env.example .env
```

至少需要配置：

```env
DATABASE_URL=postgresql+psycopg://biche:biche@localhost:5432/biche_storyboard
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
CELERY_RESULT_BACKEND=redis://localhost:6379/2

FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=
FEISHU_ROOT_FOLDER_TOKEN=root
FEISHU_WORKSPACE_PARENT_URL=https://xxx.feishu.cn/drive/folder/...
FEISHU_WORKSPACE_FOLDER_NAME=AI生成
```

如果只是本地 mock 流程，可不配置真实模型 Key；没有 Key 时会回退到 Mock Provider。

### 2.3 初始化数据库

```bash
alembic upgrade head
```

SQLite 临时调试也可以：

```bash
export DATABASE_URL='sqlite+pysqlite:///./local_storage/dev_server.db'
python scripts/init_db.py
```

### 2.4 启动 API

```bash
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

### 2.5 启动飞书长连接

如果飞书应用使用「长连接接收事件」，还要常驻运行：

```bash
python scripts/run_feishu_ws.py
```

这个进程负责：

- 接收飞书消息事件 `im.message.receive_v1`。
- 接收飞书多维表记录变更事件 `drive.file.bitable_record_changed_v1`。
- 接收飞书卡片按钮交互事件 `card.action.trigger`。

停止后，群聊命令、表格状态触发、卡片按钮都会失效。

---

## 3. 生产运行和部署

当前项目提供 macOS LaunchAgent 部署脚本：

```bash
bash scripts/deploy_runtime.sh
```

脚本会：

1. 将源码后端同步到 runtime 目录。
2. 执行 `alembic upgrade head`。
3. 重启 API LaunchAgent。
4. 重启飞书 WS LaunchAgent。
5. 运行 `scripts/self_check_runtime.py` 自检。

常用检查：

```bash
python scripts/self_check_runtime.py
python scripts/check_feishu_auth.py
python scripts/feishu_acceptance_check.py
python scripts/check_dashscope.py
```

查看 LaunchAgent：

```bash
launchctl print gui/$(id -u)/com.bicar.storyboard.api
launchctl print gui/$(id -u)/com.bicar.storyboard.feishu_ws
```

常见日志：

```text
backend/logs/backend-api.out.log
backend/logs/backend-api.err.log
backend/logs/feishu-ws.out.log
backend/logs/feishu-ws.err.log
```

---

## 4. 飞书配置

### 4.1 飞书事件订阅

飞书开发者后台至少订阅：

- `im.message.receive_v1`：机器人收到群聊/私聊消息。
- `drive.file.bitable_record_changed_v1`：多维表记录新增/编辑。
- `card.action.trigger`：卡片按钮点击。

如果只收到文字命令，但卡片按钮报 `code: 200340`，通常是 `card.action.trigger` 没订阅，或机器人卡片交互权限未启用。

### 4.2 飞书文件级订阅

多维表记录变更是云文档事件。项目创建的每个 base 会自动调用：

```text
POST /drive/v1/files/{file_token}/subscribe?file_type=bitable
```

如果没有这个文件级订阅，用户在表格里改状态，后端收不到触发。

### 4.3 飞书默认工作区

默认配置：

```env
FEISHU_WORKSPACE_PARENT_URL=父文件夹链接
FEISHU_WORKSPACE_FOLDER_NAME=AI生成
FEISHU_WORKSPACE_STORYBOARD_FOLDER_NAME=分镜项目
FEISHU_WORKSPACE_DEEP_RESEARCH_FOLDER_NAME=Deep Research
FEISHU_WORKSPACE_VIDEO_DOWNLOAD_FOLDER_NAME=视频下载
```

飞书里会形成类似结构：

```text
AI生成/
  分镜项目/
  Deep Research/
  视频下载/
  调试纸/
```

---

## 5. 飞书机器人会话模式

机器人有多个模式，同一个群聊/私聊的会话上下文彼此隔离。

### 5.1 普通助手

默认模式。直接发消息即可聊天、解释概念、做简单建议。

切回普通助手：

```text
/普通助手
```

重置当前群聊或私聊的聊天记录：

```text
/New session
```

说明：

- 只重置当前群或当前私聊，不影响其他群/私聊。
- 不删除分镜项目，不清空飞书表格。

### 5.2 Agent 模式

进入 Codex Agent：

```text
/Agent
/Codex
/智能体
```

进入 DeepSeek Agent：

```text
/Agent deepseek
/切换Agent模型 deepseek
```

通用切换格式：

```text
/切换Agent模型 codex|deepseek
```

切回 Codex Agent：

```text
/Agent codex
/切换Agent模型 codex
```

Agent 特点：

- 通过本地 OpenClaw / Codex runtime 执行。
- 群聊和私聊 session 隔离。
- 支持读取当前项目上下文，但「当前绑定项目」只是默认候选；如果用户给了新的表格链接，应以用户上下文为准。
- 遇到 `/Deep Research`、`/分镜拆解`、`/分镜助手` 等命令时，应回到项目原有功能，而不是 Agent 自己模拟一套。
- 可将最近本地产物上传到飞书，支持图片、视频、PDF、docx、txt、md 等常见格式。

停止当前 Agent 任务：

```text
/stop
/停止
```

### 5.3 分镜助手 / 视频助手

进入：

```text
/分镜助手
/视频助手
```

用于解释分镜表字段、工作流状态、报错原因、Prompt 优化建议、模型选择建议等。

注意：分镜助手本身不会假装已经执行项目操作。要真正创建项目、生成图片/视频，需要使用对应斜杠命令或表格状态。

### 5.4 Deep Research

进入：

```text
/Deep Research
```

之后发送研究主题即可，例如：

```text
请研究 e.GO 公司的发展历程、车型、融资和失败原因
```

也可以带飞书文档或文件链接：

```text
/Deep Research 请基于这个文档研究大众 ID 系列的研发过程：https://xxx.feishu.cn/docx/...
```

输出：

- 聊天里只返回摘要和文档链接。
- 研究报告保存到飞书 `AI生成/Deep Research`。
- 同时保存原始返回内容 txt，便于排查 Markdown/文档转换问题。

主链路优先级：

1. Google Gemini Deep Research。
2. OpenRouter Deep Research。
3. OpenAI Deep Research。
4. 搜索总结回退。

Gemini Deep Research 会有进度提示，长任务最多等待配置里的最大轮询时长。

### 5.5 分镜拆解

进入：

```text
/分镜拆解
/拆分镜需求
/重新拆解
```

可处理：

- 飞书 docx 链接。
- 飞书文件链接。
- 直接发送的 docx 文件。
- 直接粘贴的脚本、采访稿、策划案、PPT 提纲。

输出：

- 聊天里只给文档链接，不大段输出具体分镜内容。
- 拆解文档保存到飞书 `AI生成/分镜项目`。
- 同时保存原始 txt，便于排查。

如果用户要求「每条分镜只输出一句话」等格式，应在命令后写清楚：

```text
/分镜拆解 请把这个文档拆成分镜，每个分镜只输出一句话，不要输出表格
```

---

## 6. 常用飞书命令

### 6.1 查看帮助

```text
/help
/帮助
/菜单
```

### 6.2 新建分镜项目

默认创建到 `AI生成/分镜项目`：

```text
/新建分镜项目：测试010
```

指定创建位置：

```text
/新建分镜项目：测试010 https://xxx.feishu.cn/drive/folder/FOLDER_TOKEN
/新建分镜项目：项目名 https://xxx.feishu.cn/drive/folder/FILE_TOKEN
```

同义命令：

```text
/新建项目：项目名
/新建：项目名
/new：项目名
/新建 AI 分镜项目
```

创建后系统会：

- 新建项目文件夹。
- 新建飞书多维表格。
- 建立必要字段。
- 创建 3 行模板记录。
- 发送项目卡片，包含表格链接、文件夹链接和快捷按钮。

### 6.3 切换当前项目

```text
/切换当前项目 <飞书分镜表链接>
/切换当前项目 <表格链接>
```

作用：

- 当前群聊或私聊绑定到指定分镜表。
- 后续 `/查看进度`、Agent 默认项目上下文、批量生成命令都优先使用该项目。

### 6.4 同步表格

```text
/同步表格
```

作用：

- 从飞书多维表读取最新行。
- 补齐缺失字段和默认值。
- 删除废弃的 `镜号` 字段；镜号按实际行序自动确认。
- 将表格内容同步到本地数据库。

### 6.5 查看进度

```text
/查看进度
查询进度
```

返回：

- 项目名。
- 分镜总行数。
- Prompt 已填写数。
- 首帧/尾帧/关键帧数量。
- 图片生成状态。
- 视频生成状态。
- 首尾帧同步状态。
- 关键帧生成状态。
- 错误数量。
- 表格链接和项目文件夹链接。

---

## 7. 分镜表字段说明

分镜表由系统自动创建和维护。核心字段如下：

| 字段 | 用途 |
| --- | --- |
| 场景描述 | 用户写的粗略镜头描述，是后续 Prompt 优化基础 |
| 参考图 | 上传给模型参考的图片 |
| 参考图批注 | 说明参考图用于服装、构图、光线、机位、道具等哪一部分 |
| 关键帧提示词 | 核心画面 Prompt |
| 首帧提示词 | 镜头起始画面 Prompt |
| 尾帧提示词 | 镜头结束画面 Prompt |
| 视频 Prompt | 视频模型使用的连续镜头描述 |
| 负面 Prompt | 避免闪烁、变形、错字等 |
| 镜头运动 | 推、拉、摇、移、跟、升、降等 |
| 一致性说明 | 人物、服装、场景、光线一致性 |
| 文本模型 | 当前行 Prompt 优化模型 |
| 图片模型 | 当前行图片模型 |
| 视频模型 | 当前行视频模型 |
| 生成批次 | 批量操作筛选，默认 `batch_001` |
| 首帧同步设置 | 是否用上一镜尾帧作为本镜首帧 |
| 关键帧生成设置 | 是否生成关键帧候选 |
| 图片生成状态 | 图片生成控制：未开始/启动/正在生成/生成完成 |
| 关键帧图 | AI 生成的候选关键帧 |
| 选中关键帧图 | 视频输入用的中间关键帧，空置=不使用 |
| 关键帧时间点 | 关键帧出现在视频第几秒，允许小数 |
| 首帧图 | 视频输入首帧 |
| 尾帧图 | 视频输入尾帧 |
| 审核状态 | 人工审核记录 |
| 生成状态 | 视频生成控制：未开始/启动/正在生成/生成完成 |
| 驳回原因 | 驳回后重新生成参考 |
| 需要重新生成的选项 | 指定重做 Prompt、图片或视频 |
| 重新生成状态 | 选择启动后执行重生成 |
| 视频链接 | 视频结果链接 |
| 视频时长 | 单镜视频总长度，默认 5 秒，允许小数 |
| 满意度 | 满意/不满意 |
| 归档链接 | 复用路径 |
| 错误信息 | 失败排查 |
| Prompt 版本 | 防止旧任务覆盖新内容 |
| 任务 ID | 模型任务追踪 |
| 视频存储位置 | 可选：指定视频输出飞书文件夹 |

重要规则：

- `镜号` 字段已废弃，镜头编号按表格实际行数确认。
- 表格中的模型字段可覆盖项目默认模型。
- 表格里手动设置开关后，卡片按钮再点击会以最后一次操作为准。
- 进度中如果被手动操控，会尽量记录为自定义状态。

---

## 8. 图片和视频生成流程

### 8.1 Prompt 优化

```text
/优化当前批次 Prompt
```

默认优化 `batch_001`。

处理逻辑：

1. 同步飞书表格。
2. 读取 `场景描述`、参考图批注、已有 Prompt。
3. 调用文本模型生成关键帧、首帧、尾帧、视频 Prompt、负面 Prompt、镜头运动和一致性说明。
4. 写回飞书表格。

### 8.2 生成全部图片

```text
/生成全部图片
```

默认生成：

- 首帧图。
- 尾帧图。

只有开启关键帧生成后才批量生成关键帧图：

```text
/启动关键帧生成
```

关闭：

```text
/关闭关键帧生成
```

### 8.3 首尾帧同步

开启：

```text
/启动首尾帧同步
```

关闭：

```text
/关闭首尾帧同步
```

规则：

- 开启后，后一镜头首帧会复用上一镜头尾帧。
- 如果上一镜尾帧不存在，当前镜会报错并写入错误信息。
- 用户在卡片和表格里都能控制，最终以最后一次操作为准。

### 8.4 生成全部视频

```text
/生成全部视频
```

规则：

- 已完成的视频默认跳过。
- 如果有首帧/尾帧/参考图，会使用图生视频或首尾帧视频链路。
- 如果没有图片，按文字生成。
- `选中关键帧图` 非空时会作为视频中间关键帧输入。
- `关键帧时间点` 控制关键帧出现在视频第几秒。
- `视频时长` 控制单镜视频总长度，默认 5 秒，允许小数。

### 8.5 图片和视频连续生成

```text
/生成全部图片和视频
```

先生成图片，再生成视频。

### 8.6 表格状态触发

除命令外，也可以在表格中改状态：

- `图片生成状态 = 启动`：启动该行图片生成。
- `生成状态 = 启动`：启动该行视频生成。
- `重新生成状态 = 启动`：按「需要重新生成的选项」执行重生成。

---

## 9. 直接生成图片/视频

不创建分镜项目，直接调用模型生成。

### 9.1 直接生成图片

```text
/直接生成图片
模型=nanobanana
提示词=夕阳下的海边咖啡馆，暖光，电影感
尺寸=16:9
```

图生图：

```text
/直接生成图片
模型=nanobanana
提示词=把这张图改成卡通海报风
参考图=https://xxx.feishu.cn/file/FILE_TOKEN
```

常用模型：

- `nanobanana`：默认优先图片模型，支持参考图。
- `gpt2`：OpenRouter 的 GPT image 模型别名。
- `wanx2.1-t2i-turbo` / `wanx-v1`：DashScope 万相。

### 9.2 直接生成视频

```text
/直接生成视频
模型=小云雀
提示词=镜头缓慢推近，蒸汽自然上升
时长=5
# 也可以使用小数，例如：时长=3.5
首帧=https://xxx.feishu.cn/file/A
尾帧=https://xxx.feishu.cn/file/B
关键帧=https://xxx.feishu.cn/file/C
关键帧时间点=2.5
```

常用模型：

- `小云雀`：默认视频模型。
- `wan2.2-kf2v-flash`
- `wanx2.1-kf2v-plus`
- `wanx2.1-i2v-turbo`
- `seedance_2_0`

---

## 10. 视频下载工作流

进入方式：

```text
/下载视频 https://www.youtube.com/watch?v=...
```

或：

```text
/视频下载 https://www.youtube.com/watch?v=...
```

系统会：

1. 确保飞书 `AI生成/视频下载` 文件夹存在。
2. 确保「视频下载」多维表存在。
3. 将下载任务写入表格。
4. 下载视频，默认尽量最高画质。
5. 上传到飞书目标子文件夹。
6. 回填下载状态、文件位置、目标文件夹、log。

表格状态：

- `未开始`
- `启动`
- `正在下载`
- `已下载`
- `下载失败`

重试：

- 下载失败自动重试 3 次。
- 失败日志保留完整原因，最长保留较长文本用于排查。

命名规则：

1. 用户明确指定文件名。
2. 同一条批注/评论中，链接旁边的人工文字。
3. YouTube 原标题。
4. 批注内容。

文档批注里的视频：

- Agent 应读取飞书文档正文和批注/评论。
- 对 YouTube video_id 或规范化 URL 去重。
- 将唯一链接登记到视频下载表。
- 来自文档时，`comments` 应包含来源文档、文档链接、批注 ID、批注位置、批注说明、原始链接。
- 每个来源文档对应一个子文件夹，文件夹名应为文档原名。
- 非文档来源进入 `非文档视频下载` 文件夹。

整理历史下载：

```bash
PYTHONPATH=. python scripts/organize_video_downloads.py
```

---

## 11. 视频拆分镜

显式命令：

```text
/视频拆分镜 视频=https://xxx.feishu.cn/file/FILE_TOKEN 项目名=萨博900广告
/视频拆分镜 视频=https://xxx.feishu.cn/file/FILE_TOKEN 项目名=项目名
```

同义命令：

```text
/视频转分镜
/从视频生成分镜表
/视频生成分镜表
```

可选参数：

```text
镜头数=12
抽帧数=16
目录=https://xxx.feishu.cn/drive/folder/FOLDER_TOKEN
```

完整示例：

```text
/视频拆分镜 ... 镜头数=10 抽帧数=12 目录=https://xxx.feishu.cn/drive/folder/FOLDER_TOKEN
```

流程：

1. 下载飞书视频文件或可下载 http/https 视频。
2. 用 `ffmpeg` 抽取关键画面。
3. 调用视觉模型分析画面。
4. 生成镜头级结构。
5. 创建飞书分镜项目。
6. 写入分镜表。

注意：

- 该功能只创建/填写分镜表。
- 不会自动生成图片或视频。
- 用户应先检查表格，再执行 `/生成全部图片` 或表格状态启动生成。

---

## 12. Deep Research

### 12.1 使用方式

```text
/Deep Research
请研究某个公司、产品、市场或技术问题
```

或一条消息带主题：

```text
/Deep Research 请研究 e.GO 公司的发展历程、车型、融资和失败原因
```

带文档：

```text
/Deep Research 请基于这个飞书文档做研究：https://xxx.feishu.cn/docx/...
```

### 12.2 输出结果

系统会保存：

- `.docx` 研究报告。
- `.txt` 原始返回内容。

默认保存到：

```text
AI生成/Deep Research
```

### 12.3 进度和回退

- 开始时先回复「已开始研究，正在执行中」。
- Gemini Deep Research 进行中会定期提示状态。
- 主链路失败时会记录错误并回退。
- 最终报告不应只是一堆链接；系统会对过短、只有链接、缺少正文的结果做质量检查和回退。

---

## 13. 调试纸二维码 / 飞书原生表单

获取入口：

```text
/调试纸二维码
```

用户流程：

1. 打开或扫码飞书原生表单。
2. 登录飞书。
3. 填写「新文档名称」。
4. 第二行「生成后的文档打开链接」不用填。
5. 提交。
6. 后端复制 `调试纸CN.docx`，用用户填写的名称命名。
7. 系统把新文档链接写回表格。

保存位置：

```text
AI生成/调试纸
```

权限：

- 新生成文档会设置为公司内链接可编辑。
- 外部访问关闭。
- 外部邀请关闭。

---

## 14. 模型配置

### 14.1 默认模型

默认 Provider：

```env
DEFAULT_TEXT_PROVIDER=deepseek
DEFAULT_IMAGE_PROVIDER=openrouter
DEFAULT_VIDEO_PROVIDER=xyq_nest
```

默认模型含义：

- 文本：DeepSeek。
- 图片：OpenRouter 上的 `nanobanana`。
- 视频：小云雀。

### 14.2 文本模型

支持：

- `qwen-plus`
- `qwen-max`
- `gpt-5.4`
- `deepseek-v4-pro`
- `deepseek-v4-flash`
- `google/gemini-3.1-pro-preview`
- `google/gemini-3.1-flash-lite-preview`

切换普通聊天模型：

```text
/切换chatbot模型 deepseek-v4-pro
/切换chatbot模型 qwen-plus
```

联网搜索：

- OpenRouter Gemini 聊天模型会按需接入 web search。
- DeepSeek 聊天模型也会在需要时先做公开网页搜索，再把结果提供给模型。

### 14.3 图片模型

表格和直接生成支持：

- `nanobanana`
- `gpt2`
- `wanx2.1-t2i-turbo`
- `wanx-v1`

兼容别名：

- `neobunana` 会归一为 `nanobanana`。
- `google/gemini-3.1-flash-image-preview` 会归一为 `nanobanana`。
- `openai/gpt-5.4-image-2` 会归一为 `gpt2`。

### 14.4 视频模型

支持：

- `小云雀`
- `wan2.2-kf2v-flash`
- `wanx2.1-kf2v-plus`
- `wanx2.1-i2v-turbo`
- `seedance_2_0`

兼容别名：

- `xyq`
- `xyq_nest`
- `xiaoyunque`
- `xiao_yunque`

都会归一为：

```text
小云雀
```

### 14.5 小云雀注意事项

- 小云雀用于视频生成。
- 失败时会记录错误信息到飞书表格。
- 限流或类似错误会进入回退等待和重试。
- 真人图片上传可能因平台限制失败，遇到这种情况应检查参考图是否包含真人。

---

## 15. Agent 与 OpenClaw

Agent 模式通过 OpenClaw 调用本地 Codex / DeepSeek runtime。

常用维护命令：

```bash
openclaw status
openclaw models status
openclaw models auth login --provider openai-codex --set-default
```

项目代码优先使用 bundled OpenClaw：

```text
~/.openclaw/tools/node-v22.22.0/bin/node
~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js
```

验证 Codex Agent：

```bash
~/.openclaw/tools/node-v22.22.0/bin/node \
  ~/.openclaw/tools/node-v22.22.0/lib/node_modules/openclaw/dist/index.js \
  agent \
  --session-id codex-smoke \
  --message '请只回复：Codex Agent OK' \
  --model openai/gpt-5.5 \
  --json \
  --timeout 180
```

如果报：

```text
Your session has ended. Please log in again.
```

说明 OpenAI Codex OAuth 过期，需要重新登录：

```bash
openclaw models auth login --provider openai-codex --set-default
```

---

## 16. API 路由概览

FastAPI 路由主要包括：

- `/api/projects`：项目相关 API。
- `/api/shots`：镜头相关 API。
- `/api/jobs`：任务相关 API。
- `/api/metrics`：指标。
- `/webhooks/feishu/events`：飞书事件。
- `/webhooks/feishu/bitable-trigger`：多维表触发。
- `/webhooks/provider/...`：模型 provider webhook。
- `/tools/debug-paper-copy`：调试纸网页工具页。
- `/tools/debug-paper-copy/qr`：调试纸二维码。

实际路由以 `app/api/router.py` 和 `app/api/routes/` 为准。

---

## 17. 常用脚本

### 17.1 自检

```bash
python scripts/self_check_runtime.py
python scripts/check_feishu_auth.py
python scripts/feishu_acceptance_check.py
python scripts/check_dashscope.py
```

### 17.2 飞书长连接

```bash
python scripts/run_feishu_ws.py
```

### 17.3 部署

```bash
bash scripts/deploy_runtime.sh
```

### 17.4 视频下载整理

```bash
PYTHONPATH=. python scripts/organize_video_downloads.py
```

### 17.5 调试项目进度

仓库里存在若干临时脚本，例如：

- `scripts/check_progress.py`
- `scripts/check_status.py`
- `scripts/check_errors.py`
- `scripts/check_video_progress.py`
- `scripts/read_table_records.py`

这些脚本多用于现场排查，使用前应先读脚本内容，确认目标项目/表格是否写死，避免误操作。

---

## 18. 测试

运行全部测试：

```bash
cd backend
.venv311/bin/pytest
```

常用专项：

```bash
.venv311/bin/pytest tests/test_bot_help.py
.venv311/bin/pytest tests/test_chatbot_memory.py
.venv311/bin/pytest tests/test_debug_paper_form.py
.venv311/bin/pytest tests/test_video_downloads.py
.venv311/bin/pytest tests/test_video_storyboard.py
.venv311/bin/pytest tests/test_feishu_storyboard_flow.py
```

测试覆盖：

- 帮助卡片和命令解析。
- 聊天记忆隔离。
- Agent / 普通助手 / Deep Research / 分镜拆解模式。
- 调试纸表单和回填。
- 视频下载表格、命名、重试、整理。
- 视频拆分镜。
- 飞书分镜表字段和生成流程。
- 模型路由和限流重试。

---

## 19. 常见问题

### 19.1 飞书机器人不回复

检查：

1. `scripts/run_feishu_ws.py` 是否在运行。
2. 飞书应用是否订阅 `im.message.receive_v1`。
3. `.env` 里的 `FEISHU_APP_ID`、`FEISHU_APP_SECRET` 是否正确。
4. 日志 `logs/feishu-ws.err.log` 是否有异常。

### 19.2 表格改了状态但没有生成

检查：

1. 是否订阅 `drive.file.bitable_record_changed_v1`。
2. 新建 base 是否完成文件级 subscribe。
3. 状态字段是否是 `启动`。
4. 当前项目是否已绑定或能通过表格链接找到。
5. 日志里是否有飞书权限错误。

### 19.3 生成状态一直停在「启动」

通常说明表格事件没有触发，或触发后生成任务失败。检查：

- 飞书 WS 是否运行。
- 表格事件订阅是否生效。
- `错误信息` 字段。
- 后端日志。

### 19.4 OpenRouter 报 `402 Payment Required`

表示 OpenRouter 余额不足或该模型不可用。解决：

- 充值 OpenRouter。
- 切换图片模型。
- 对失败行重新启动生成。

### 19.5 小云雀提示非 VIP 或权限限制

说明当前小云雀账号或接口能力被限制。需要检查：

- `XYQ_ACCESS_KEY` 是否正确。
- 小云雀账号权限。
- 是否上传了平台不允许的真人图片。

### 19.6 Agent 返回兜底消息

兜底消息表示 OpenClaw 没有返回最终可发送正文，不等于一定没执行动作。

处理：

1. 先检查飞书表格/文件夹是否已经发生变化。
2. 看 OpenClaw 日志。
3. 如果提示超时、限流、额度错误，应按具体原因处理。
4. 必要时 `/New Session` 重置当前会话。

### 19.7 Deep Research 只返回链接没有正文

系统会尽量识别低质量报告并回退；如果仍出现：

- 查看同目录保存的原始 txt。
- 检查 Deep Research provider 是否异常。
- 换模型或重新执行。

### 19.8 文档 Markdown 表格格式不好

Deep Research 和分镜拆解会先把 Markdown 渲染成 docx 再上传飞书，减少飞书 Markdown 解析问题。若仍异常，查看同时保存的原始 txt。

---

## 20. 安全和提交规范

不要提交：

- `.env`
- API Key
- 飞书 app secret
- 本地数据库
- `local_storage/` 里的运行数据
- `logs/`
- 临时生成的大文件

建议提交前运行：

```bash
git status --short
.venv311/bin/pytest
```

如果只改文档，可至少确认：

```bash
git diff -- README.md
```

---

## 21. 推荐使用流程

### 21.1 从零创建分镜视频

1. 群里发送：

   ```text
   /新建分镜项目：项目名
   ```

2. 打开飞书表格，填写 `场景描述` 和参考图。
3. 发送：

   ```text
   /优化当前批次 Prompt
   ```

4. 检查 Prompt。
5. 需要连续镜头一致性时：

   ```text
   /启动首尾帧同步
   ```

6. 需要关键帧时：

   ```text
   /启动关键帧生成
   ```

7. 生成图片：

   ```text
   /生成全部图片
   ```

8. 检查首帧、尾帧、关键帧。
9. 生成视频：

   ```text
   /生成全部视频
   ```

10. 用 `/查看进度` 检查结果和错误。

### 21.2 根据已有视频生成分镜表

```text
/视频拆分镜 视频=https://xxx.feishu.cn/file/FILE_TOKEN 项目名=项目名 镜头数=12 抽帧数=16
```

完成后先人工检查表格，再启动图片/视频生成。

### 21.3 下载文档批注里的 YouTube 视频

在 Agent 中说明需求，例如：

```text
/Agent
请读取这个文档批注里的所有 YouTube 链接，登记到视频下载工作流并下载到对应文档文件夹：https://xxx.feishu.cn/docx/...
```

Agent 应把任务登记到正式「视频下载」表，而不是自己临时下载到本地。

### 21.4 生成研究报告

```text
/Deep Research
请研究某公司/车型/技术路线/市场竞争格局，并输出报告
```

等待返回飞书文档链接。

### 21.5 复制调试纸模板

```text
/调试纸二维码
```

打开表单，填写新文档名称，提交后在第二行或处理记录表查看新文档链接。
