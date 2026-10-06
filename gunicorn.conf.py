import os

# Render dynamically assigns $PORT. Gunicorn will automatically bind to 0.0.0.0:$PORT
port = os.environ.get("PORT", "5000")
bind = f"0.0.0.0:{port}"
workers = int(os.environ.get("WEB_CONCURRENCY", "1"))
worker_class = "gthread"
threads = 4
timeout = 120
keepalive = 5
preload_app = False
