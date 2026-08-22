from celery import Celery

from vyom.config import settings

celery_app = Celery(
    "vyom",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["vyom.tasks"],
)

celery_app.conf.update(
    task_routes={
        "vyom.download.*": {"queue": "download"},
        "vyom.process.*": {"queue": "process"},
        "vyom.stats.*": {"queue": "stats"},
        "vyom.discovery.*": {"queue": "discover"},
    },
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Celery beat schedule: periodic discovery sweep across all registered farms.
# (Per-farm on-demand discovery is also available via the /farms/{id}/refresh
# API endpoint — this beat schedule is for "keep everything fresh" background
# polling once you have many farms registered.)
celery_app.conf.beat_schedule = {
    "poll-all-farms-daily": {
        "task": "vyom.discovery.poll_all_farms",
        "schedule": 6 * 60 * 60,  # every 6 hours; Sentinel-2 revisit is ~5 days so this is generous headroom
    },
}
