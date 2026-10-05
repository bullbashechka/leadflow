"""Bounded production workers; Django validates the trusted proxy metadata."""

import os

bind = f"0.0.0.0:{int(os.environ.get('PORT', '8000'))}"
workers = 2
worker_class = "gthread"
threads = 2
timeout = 30
graceful_timeout = 30
accesslog = "-"
errorlog = "-"
# Keep search queries and request bodies out of the access log.
access_log_format = "%(h)s %(m)s %(U)s %(s)s %(L)s"
# Only TrustedProxyMiddleware may determine HTTPS from forwarded metadata.
secure_scheme_headers = {}
