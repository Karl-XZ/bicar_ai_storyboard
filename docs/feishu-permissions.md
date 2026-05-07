# 飞书上线权限与回调配置

真实验收按“飞书机器人 + 飞书分镜表”流程执行，飞书开放平台应用必须先开通下面的应用身份权限，并发布/管理员审批后生效。

## 必需权限

- `im:message:send` 或 `im:message:send_as_bot`：机器人向群或私聊发送文本、卡片、进度和失败提醒。
- `space:folder:create` 或 `drive:drive`：创建项目文件夹、参考图文件夹、帧图文件夹、视频文件夹和归档文件夹。
- `base:app:create` 或 `bitable:app`：创建多维表格应用。
- `bitable:app`：创建数据表、字段、读取记录、批量回填记录。
- `drive:drive.upload` 或对应文件上传权限：上传首帧图、尾帧图、关键帧图和视频文件。
- `drive:drive.metadata:readonly`：读取根目录或文件夹元信息时需要。

## 事件订阅

在飞书开放平台后台配置请求地址：

- 事件回调：`POST https://你的公网域名/api/webhooks/events`
- 卡片回调：`POST https://你的公网域名/api/webhooks/card-actions`
- 多维表格自动化触发器：`POST https://你的公网域名/api/webhooks/bitable-trigger`

当前代码已支持 `Verification Token` 校验和 `Encrypt Key` 解密。飞书后台 URL 校验请求会返回 `challenge`。

## 本地验收命令

```bash
cd backend
python scripts/check_feishu_auth.py
python scripts/feishu_acceptance_check.py
pytest -q
```

`feishu_acceptance_check.py` 会真实调用飞书 API 检查机器人发消息、创建文件夹、创建多维表格的权限。若权限缺失，输出里会列出飞书返回的 `required_scopes`。
