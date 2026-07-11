from fastapi import APIRouter

from app.api.routes import health, jobs, metrics, projects, provider_webhooks, shots, tools, webhooks

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(metrics.router)
api_router.include_router(projects.router, prefix="/api/projects", tags=["projects"])
api_router.include_router(shots.router, prefix="/api/shots", tags=["shots"])
api_router.include_router(jobs.router, prefix="/api/jobs", tags=["jobs"])
api_router.include_router(webhooks.router, prefix="/webhooks/feishu", tags=["feishu-webhooks"])
api_router.include_router(webhooks.router, prefix="/api/webhooks", tags=["feishu-webhooks"])
api_router.include_router(provider_webhooks.router, prefix="/webhooks/providers", tags=["provider-webhooks"])
api_router.include_router(tools.router, tags=["tools"])
