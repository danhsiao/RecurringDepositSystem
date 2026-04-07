import os
import ssl
from celery import Celery
from dotenv import load_dotenv

load_dotenv()


def _ensure_ssl_param(url: str) -> str:
    """
    Celery's Redis backend URL parser requires ssl_cert_reqs to be present
    in the URL itself when using rediss://.  Append it if missing.
    """
    if url.startswith("rediss://") and "ssl_cert_reqs" not in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}ssl_cert_reqs=CERT_NONE"
    return url


BROKER_URL     = _ensure_ssl_param(os.environ["CELERY_BROKER_URL"])
RESULT_BACKEND = _ensure_ssl_param(os.environ["CELERY_RESULT_BACKEND"])

celery_app = Celery(
    "recurring_deposit",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.tasks"],
)

# ssl.CERT_NONE tells the Redis client not to verify Upstash's certificate chain.
# This is required for Upstash's managed TLS endpoint.
_ssl_opts = {"ssl_cert_reqs": ssl.CERT_NONE}

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_use_ssl=_ssl_opts,
    redis_backend_use_ssl=_ssl_opts,
)
