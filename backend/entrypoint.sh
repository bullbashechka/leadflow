#!/bin/sh
set -eu

if [ "${DJANGO_SETTINGS_MODULE:-}" = "config.settings.production" ]; then
    python manage.py check --deploy --fail-level ERROR
    if [ "${1:-}" = "gunicorn" ]; then
        python manage.py collectstatic --noinput
    fi
fi

exec "$@"
