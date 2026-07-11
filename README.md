# 哔车 AI 助手 / AI 分镜工作流

哔车 AI 助手是一套围绕飞书搭建的 AI 视频创作与分镜管理系统。它把飞书机器人、飞书多维表格、飞书云文档、图片/视频生成模型、Deep Research、Agent 工作流和视频下载流程整合在一起，用于从脚本、文档、参考视频或聊天需求快速生成可管理的分镜项目。

> 详细部署、配置、命令和排障说明见：[后端完整中文使用手册](backend/README.md)。

## 核心能力

- 飞书机器人普通聊天、Agent、Deep Research、分镜助手、分镜拆解多模式会话。
- 一条命令创建飞书分镜项目，自动生成项目文件夹和多维表格。
- 在飞书多维表格里维护镜头描述、参考图、Prompt、首帧、尾帧、关键帧、视频状态和错误日志。
- 调用文本、图片、视频模型完成 Prompt 优化、首帧/尾帧/关键帧生成和视频生成。
- 支持 OpenRouter、DeepSeek、OpenAI、DashScope、Google Gemini、小云雀、Seedance 等模型接入。
- 支持 Deep Research 联网调研，并把研究结果保存成飞书文档。
- 支持把飞书文档、docx 文件或脚本文字拆解成镜头级分镜需求。
- 支持视频下载工作流，把 YouTube/外部视频下载到飞书 `AI生成/视频下载` 并登记状态。
- 支持视频拆分镜：下载视频、抽帧、视觉模型分析，并生成可编辑分镜表。
- 支持调试纸二维码：飞书原生表单提交名称后自动复制 `调试纸CN.docx` 并回填链接。

## 演示截图

下面是一组从飞书移动端截取的完整流程示例：从 Agent 能力说明、文档/分镜需求发起，到项目创建、分镜表编辑、图片/视频生成与进度反馈。

<table>
  <tr>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/01-agent-capabilities.jpg" alt="Agent 能力说明" width="100%">
      <br>
      <sub>Agent 能力说明</sub>
    </td>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/02-doc-to-storyboard-request.jpg" alt="基于文档发起分镜制作需求" width="100%">
      <br>
      <sub>基于文档发起分镜制作需求</sub>
    </td>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/03-project-created-card.jpg" alt="分镜项目创建成功卡片" width="100%">
      <br>
      <sub>分镜项目创建成功卡片</sub>
    </td>
  </tr>
  <tr>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/04-storyboard-table-list.jpg" alt="飞书多维表分镜列表" width="100%">
      <br>
      <sub>飞书多维表分镜列表</sub>
    </td>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/05-shot-prompt-detail.jpg" alt="单镜头描述与 Prompt 字段" width="100%">
      <br>
      <sub>单镜头描述与 Prompt 字段</sub>
    </td>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/06-generation-settings.jpg" alt="模型、首尾帧与生成设置" width="100%">
      <br>
      <sub>模型、首尾帧与生成设置</sub>
    </td>
  </tr>
  <tr>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/07-video-result-status.jpg" alt="视频生成结果与状态" width="100%">
      <br>
      <sub>视频生成结果与状态</sub>
    </td>
    <td width="33%" align="center">
      <img src="docs/assets/readme-demo/08-agent-progress-card.jpg" alt="Agent 生成进度反馈" width="100%">
      <br>
      <sub>Agent 生成进度反馈</sub>
    </td>
    <td width="33%" align="center">
      <em>更多示例可继续补充</em>
    </td>
  </tr>
</table>

## 最常用命令

```text
/help
/Agent
/Agent deepseek
/普通助手
/New session
/Deep Research
/分镜拆解
/分镜助手
/视频助手
/调试纸二维码
/新建分镜项目：项目名
/新建分镜项目：项目名 https://xxx.feishu.cn/drive/folder/FILE_TOKEN
/切换当前项目 <表格链接>
/视频拆分镜 视频=https://xxx.feishu.cn/file/FILE_TOKEN 项目名=项目名
/优化当前批次 Prompt
/生成全部图片
/生成全部视频
/生成全部图片和视频
/启动首尾帧同步
/关闭首尾帧同步
/启动关键帧生成
/关闭关键帧生成
/直接生成图片
/直接生成视频
/同步表格
/查看进度
```

## 推荐使用流程

1. 在飞书群或私聊里发送 `/新建分镜项目：项目名`。
2. 打开自动创建的飞书多维表，填写每个镜头的 `场景描述`、参考图和参考图批注。
3. 发送 `/优化当前批次 Prompt`，让文本模型补齐关键帧、首帧、尾帧和视频 Prompt。
4. 按需要开启 `/启动首尾帧同步` 或 `/启动关键帧生成`。
5. 发送 `/生成全部图片`，审核首帧、尾帧和关键帧。
6. 发送 `/生成全部视频` 或在表格里把单条 `生成状态` 改成 `启动`。
7. 用 `/查看进度` 检查生成状态、错误信息、视频链接和归档结果。

## 项目结构

```text
backend/
  app/
    adapters/          # 飞书 API、卡片、字段定义、签名等适配层
    api/routes/        # FastAPI HTTP 路由、飞书 webhook、工具页
    core/              # 配置、日志、模型别名
    db/                # SQLAlchemy session
    domain/            # 枚举和 Pydantic schema
    models/            # 数据库模型
    providers/         # 模型 Provider
    services/          # 核心业务：机器人命令、分镜、视频下载、Deep Research 等
    workers/           # Celery 任务
  alembic/             # 数据库迁移
  scripts/             # 本地运维、验收、部署、调试脚本
  tests/               # 单元测试和流程测试
docs/assets/           # README 演示截图等文档素材
```

## 本地启动

```bash
cd backend
python3 -m venv .venv311
source .venv311/bin/activate
pip install -r requirements.txt
cp .env.example .env
alembic upgrade head
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

飞书长连接事件接收：

```bash
python scripts/run_feishu_ws.py
```

更多配置项、飞书权限、模型 Key、LaunchAgent 部署和常见问题请阅读：[backend/README.md](backend/README.md)。

## 测试

```bash
cd backend
python -m pytest
```

当前主测试覆盖飞书命令、会话隔离、分镜表字段、图片/视频生成状态、视频下载、视频拆分镜、调试纸表单和模型回退等核心流程。

## 安全说明

不要提交 `.env`、API Key、飞书密钥、本地数据库、日志、缓存、虚拟环境或生成产物。仓库中的 `.env.example` 只保留空值和示例配置。
