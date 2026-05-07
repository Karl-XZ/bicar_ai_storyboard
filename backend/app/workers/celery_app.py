from celery import Celery

from app.core.config import settings

celery_app = Celery(
    "biche_storyboard",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.task_routes = {
    "app.workers.tasks.optimize_prompt": {"queue": "prompt.optimize"},
    "app.workers.tasks.generate_image": {"queue": "image.generate"},
    "app.workers.tasks.generate_video": {"queue": "video.generate"},
    "app.workers.tasks.poll_video": {"queue": "provider.polling"},
    "app.workers.tasks.backfill_feishu": {"queue": "feishu.backfill"},
    "app.workers.tasks.archive_video": {"queue": "archive"},
}

