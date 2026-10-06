# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=config.settings.production

COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
        SSL_CERT_FILE=/run/secrets/proxy_ca uv sync --locked --no-dev --no-cache; \
    else uv sync --locked --no-dev --no-cache; fi
COPY . .
RUN useradd --create-home --uid 1000 app && chmod +x /app/entrypoint.sh \
    && chown -R app:app /app
USER app
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["gunicorn", "--config", "config/gunicorn.py", "config.wsgi:application"]

FROM base AS development
USER root
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
        SSL_CERT_FILE=/run/secrets/proxy_ca uv sync --locked --no-cache; \
    else uv sync --locked --no-cache; fi
USER app
ENV DJANGO_SETTINGS_MODULE=config.settings.local
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

FROM base AS production
