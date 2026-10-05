import json
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.utils import timezone

from leadflow.bot.models import BotPollingState
from leadflow.bot.models import BotUser
from leadflow.bot.models import Draft
from leadflow.bot.models import OutboundMessage
from leadflow.bot.models import ProcessedUpdate


def status(**options):
    output = StringIO()
    call_command("release_status", stdout=output, **options)
    return json.loads(output.getvalue())


@pytest.mark.django_db
def test_status_reports_queue_age_and_backlog_without_personal_data():
    now = timezone.now()
    polling = BotPollingState.objects.create(bot_id=998811)
    processed = ProcessedUpdate.objects.create(polling_state=polling, update_id=556677)
    user = BotUser.objects.create(telegram_id=777888, username="private_username")
    Draft.objects.create(user=user, submission_state="pending")
    for ordinal, message_status, due in (
        (0, "pending", now - timedelta(minutes=1)),
        (1, "pending", now + timedelta(minutes=1)),
        (2, "failed", now),
        (3, "delivered", now),
    ):
        OutboundMessage.objects.create(
            processed_update=processed,
            ordinal=ordinal,
            chat_id=12345678,
            text="private_text",
            reply_markup={"private": "private_markup"},
            status=message_status,
            next_attempt_at=due,
            last_error="private_error",
        )
    OutboundMessage.objects.filter(status="pending").update(created_at=now - timedelta(seconds=180))

    result = status()

    assert result["bot"]["pending"] == 2
    assert result["bot"]["due"] == 1
    assert result["bot"]["failed"] == 1
    assert 180 <= result["bot"]["oldest_pending_age_seconds"] < 185
    assert result["bot"]["pending_submissions"] == 1
    assert result["database"] == "ok"
    serialized = json.dumps(result)
    for private in (
        "private_username",
        "private_text",
        "private_markup",
        "private_error",
        "12345678",
        "998811",
        "556677",
        "777888",
    ):
        assert private not in serialized
    assert OutboundMessage.objects.count() == 4
    assert Draft.objects.count() == 1


@pytest.mark.django_db
def test_status_of_empty_queue_is_ready():
    result = status()
    assert result["database"] == "ok"
    assert result["bot"]["oldest_pending_age_seconds"] == 0
    assert result["bot"]["pending"] == 0


@pytest.mark.django_db
def test_status_fails_safely_when_database_is_unavailable():
    output = StringIO()
    with patch("django.db.backends.postgresql.base.DatabaseWrapper.cursor") as cursor:
        cursor.side_effect = OperationalError("private_database_connection")
        with pytest.raises(CommandError) as error:
            call_command("release_status", stdout=output)
    assert "private_database_connection" not in str(error.value)
    assert output.getvalue() == ""


@pytest.mark.django_db
def test_status_cooldown_is_reported_without_bot_identity():
    now = timezone.now()
    BotPollingState.objects.create(bot_id=987654, outbound_retry_at=now + timedelta(seconds=60))
    result = status()
    assert 55 <= result["bot"]["cooldown_seconds"] <= 60
    assert "987654" not in json.dumps(result)


@pytest.mark.django_db
def test_status_age_limit_returns_nonzero_with_metrics_available():
    polling = BotPollingState.objects.create(bot_id=1)
    processed = ProcessedUpdate.objects.create(polling_state=polling, update_id=1)
    message = OutboundMessage.objects.create(
        processed_update=processed,
        ordinal=0,
        chat_id=1,
        text="private",
        next_attempt_at=timezone.now(),
    )
    OutboundMessage.objects.filter(pk=message.pk).update(
        created_at=timezone.now() - timedelta(seconds=120)
    )
    output = StringIO()
    with pytest.raises(CommandError, match="Pending delivery age"):
        call_command("release_status", stdout=output, max_pending_age=60)
    assert json.loads(output.getvalue())["bot"]["pending"] == 1
