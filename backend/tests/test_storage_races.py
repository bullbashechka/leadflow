from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from threading import Event
from threading import local
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import close_old_connections
from django.db import transaction

from leadflow.bot.models import Draft
from leadflow.bot.services import cancel_draft
from leadflow.bot.services import confirm_draft
from leadflow.bot.services import get_dialogue
from leadflow.bot.services import set_field
from leadflow.bot.services import start_draft
from leadflow.crm.models import Lead
from leadflow.crm.models import SubmissionReceipt
from leadflow.crm.models import Tag
from leadflow.crm.services import StaleDraft
from leadflow.crm.services import SubmissionConflict
from leadflow.crm.services import create_lead

pytestmark = pytest.mark.django_db(transaction=True)


def run_race(first, second):
    barrier = Barrier(2)

    def invoke(operation):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                return operation()
            except (StaleDraft, SubmissionConflict) as error:
                return error
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(invoke, [first, second]))


def make_review():
    Tag.objects.get_or_create(code="website", defaults={"name": "Сайт"})
    draft = start_draft(42)
    for field, value in [
        ("name", "Клиент"),
        ("contacts", "@alexander"),
        ("continue_contacts", None),
        ("direction", "website"),
        ("request", "Нужен сайт"),
    ]:
        draft = set_field(42, draft.pk, draft.revision, field, value)
    return draft


def test_concurrent_confirmations_create_one_lead():
    draft = make_review()
    results = run_race(
        lambda: confirm_draft(42, draft.pk, draft.revision),
        lambda: confirm_draft(42, draft.pk, draft.revision),
    )
    assert results[0].lead.pk == results[1].lead.pk
    assert sorted(result.replayed for result in results) == [False, True]
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
    assert not Draft.objects.exists()


@pytest.mark.parametrize("action", ["cancel", "restart"])
def test_confirmation_racing_with_cancel_or_restart_has_one_consistent_outcome(action):
    draft = make_review()
    other = (
        (lambda: cancel_draft(42, draft.pk, draft.revision))
        if action == "cancel"
        else (lambda: start_draft(42, restart=True))
    )
    run_race(lambda: confirm_draft(42, draft.pk, draft.revision), other)
    state = get_dialogue(42)
    assert Lead.objects.count() == SubmissionReceipt.objects.count()
    assert Lead.objects.count() in {0, 1}
    if Lead.objects.exists():
        assert state.last_receipt.lead_id == Lead.objects.get().pk
    else:
        assert state.last_receipt is None
    assert not Draft.objects.filter(pk=draft.pk).exists()
    if action == "restart":
        assert state.draft is not None and state.draft.values == {}
    else:
        assert state.draft is None


def test_concurrent_draft_start_resumes_one_draft():
    drafts = run_race(lambda: start_draft(42), lambda: start_draft(42))
    assert drafts[0].pk == drafts[1].pk
    assert Draft.objects.count() == 1


def test_concurrent_different_payload_for_same_uuid_returns_conflict():
    submission_id = uuid4()
    payload = {"name": "Клиент", "contacts": ["@alexander"], "request": "Нужен сайт"}
    results = run_race(
        lambda: create_lead(submission_id, payload),
        lambda: create_lead(submission_id, payload | {"request": "Нужна реклама"}),
    )
    assert sum(isinstance(result, SubmissionConflict) for result in results) == 1
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1


def test_contender_can_save_when_the_first_submission_rolls_back():
    submission_id = uuid4()
    payload = {"name": "Клиент", "contacts": ["@alexander"], "request": "Нужен сайт"}
    first_written = Event()
    contender_at_insert = Event()
    worker = local()
    original_create = SubmissionReceipt.objects.create

    def insert_receipt(**kwargs):
        if worker.role == "contender":
            contender_at_insert.set()
        return original_create(**kwargs)

    def roll_back():
        worker.role = "first"
        close_old_connections()
        try:
            with transaction.atomic():
                create_lead(submission_id, payload)
                first_written.set()
                assert contender_at_insert.wait(timeout=10)
                raise RuntimeError("Simulated failure before commit")
        except RuntimeError:
            return "rolled_back"
        finally:
            close_old_connections()

    def contender():
        worker.role = "contender"
        close_old_connections()
        try:
            assert first_written.wait(timeout=10)
            return create_lead(submission_id, payload)
        finally:
            close_old_connections()

    with patch(
        "leadflow.crm.services.SubmissionReceipt.objects.create", side_effect=insert_receipt
    ):
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(roll_back)
            second = executor.submit(contender)
            assert first.result(timeout=15) == "rolled_back"
            result = second.result(timeout=15)
    assert not result.replayed
    assert Lead.objects.count() == SubmissionReceipt.objects.count() == 1
