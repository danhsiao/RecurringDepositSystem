import os
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

BROKER_URL = os.environ["CELERY_BROKER_URL"]
RESULT_BACKEND = os.environ["CELERY_RESULT_BACKEND"]

celery_app = Celery(
    "recurring_deposit",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks"],
)

celery_app.conf.update(
    # Use JSON so tasks are human-readable in the queue
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Upstash Redis requires SSL; these settings keep TLS connections healthy
    broker_use_ssl={"ssl_cert_reqs": None} if BROKER_URL.startswith("rediss://") else None,
    redis_backend_use_ssl={"ssl_cert_reqs": None} if RESULT_BACKEND.startswith("rediss://") else None,
    # Retry a failed task up to 3 times with exponential back-off
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)
