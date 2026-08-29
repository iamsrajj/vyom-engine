from celery import Celery

from vyom.config import settings

celery_app = Celery(
    "vyom",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["vyom.tasks"],
)

# Every pipeline stage has a normal queue and a "_priority" twin. A farm-
# onboarding request (a real person waiting for their new farm's data) gets
# dispatched to the _priority queues; background work (poll_all_farms sweeps)
# always uses the plain ones. This ONLY matters if a separate worker PROCESS
# is dedicated to consuming just the _priority queues -- see
# deploy/vyom-celery-worker-priority.service. If the same worker process
# consumes both plain and _priority queues together, Redis-backed Celery
# workers interleave queues rather than strictly prioritizing one, so
# "priority" would be nominal, not real -- the dedicated worker process is
# what actually makes background work unable to block a real farmer's request.
PRIORITY_QUEUE_SUFFIX = "_priority"


def priority_queue_name(base_queue: str) -> str:
    return f"{base_queue}{PRIORITY_QUEUE_SUFFIX}"


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
        # every 6 hours; Sentinel-2 revisit is ~5 days so this is generous headroom
        "schedule": 6 * 60 * 60,
    },
}
