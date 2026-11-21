import multiprocessing
import os

# Server socket
bind = f"{os.getenv('SERVER_HOST', '0.0.0.0')}:{os.getenv('SERVER_PORT', '8000')}"

# Worker processes
# Formula: (2 x Num_Cores) + 1, or override via WEB_CONCURRENCY
default_workers = (multiprocessing.cpu_count() * 2) + 1
workers = int(os.getenv("WEB_CONCURRENCY", default_workers))
worker_class = "uvicorn.workers.UvicornWorker"

# Worker lifecycle & timeouts
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
keepalive = int(os.getenv("GUNICORN_KEEPALIVE", "65"))
max_requests = int(os.getenv("GUNICORN_MAX_REQUESTS", "1000"))
max_requests_jitter = int(os.getenv("GUNICORN_MAX_REQUESTS_JITTER", "50"))

# Logging
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" (%(D)s µs)'
