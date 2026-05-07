# 部署说明

## 本地

```powershell
docker compose up -d postgres redis minio
cd backend
python scripts/init_db.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 密钥

真实密钥只放在 `backend/.env` 或生产 Secret 管理系统中，不写入飞书表格和日志。需要配置：

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_VERIFICATION_TOKEN`
- `FEISHU_ENCRYPT_KEY`
- `OPENAI_API_KEY`
- `GOOGLE_API_KEY`
- `SEEDANCE_API_KEY`
- `SEEDANCE_BASE_URL`
- `STORAGE_*`

## 回调

飞书事件和卡片回调：

- `/webhooks/feishu/events`
- `/webhooks/feishu/card-actions`
- `/webhooks/feishu/bitable-trigger`

Provider 回调：

- `/webhooks/providers/seedance`

## 回滚

API 和 Worker 无状态，回滚镜像即可。数据库变更通过 Alembic 管理，必要时执行对应 downgrade。

