"""Synchronous transactional domain operations, shared by HTTP and bot callers."""

from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError
from django.db import transaction

from .models import Lead
from .models import LeadContact
from .models import SubmissionReceipt
from .models import Tag
from .validation import InputError
from .validation import validate_contacts
from .validation import validate_text


class SubmissionConflict(ValueError):
    pass


class SubmissionForbidden(ValueError):
    pass


class StaleDraft(ValueError):
    pass


@dataclass(frozen=True)
class SubmissionResult:
    lead: Lead
    replayed: bool


def create_lead(submission_id, payload=None, *, bot_user_id=None, draft_revision=None):
    """Commit all submission state, or replay an already committed receipt.

    Bot callers supply identity/revision only. Read values from the locked draft,
    never from an old callback or an untrusted request payload.
    """
    submission_id = parse_submission_id(submission_id)
    channel = "telegram_bot" if bot_user_id is not None else "manual"
    snapshot = _snapshot(payload) if channel == "manual" else None
    try:
        with transaction.atomic():
            receipt = (
                SubmissionReceipt.objects.select_related("lead")
                .filter(
                    pk=submission_id,
                )
                .first()
            )
            if receipt:
                return _replay(receipt, channel, bot_user_id, snapshot)
            draft = state = None
            if channel == "telegram_bot":
                from leadflow.bot.models import BotUser
                from leadflow.bot.models import Draft

                state = BotUser.objects.select_for_update().filter(pk=bot_user_id).first()
                # Another confirmation can commit while we wait for the user lock.
                receipt = (
                    SubmissionReceipt.objects.select_related("lead")
                    .filter(
                        pk=submission_id,
                    )
                    .first()
                )
                if receipt:
                    return _replay(receipt, channel, bot_user_id, None)
                draft = Draft.objects.filter(pk=submission_id).first()
                if draft and draft.user_id != bot_user_id:
                    raise SubmissionForbidden("Submission belongs to another user")
                current_revision = (
                    draft.pending_revision
                    if draft and draft.submission_state == "pending"
                    else draft.revision
                    if draft
                    else None
                )
                if (
                    not state
                    or not draft
                    or type(draft_revision) is not int
                    or current_revision != draft_revision
                    or draft.step != "review"
                    or draft.submission_state not in {"collecting", "pending"}
                ):
                    raise StaleDraft("Review is no longer current")
                directions = draft.values.get("directions")
                if directions is None and draft.values.get("direction"):
                    directions = [draft.values["direction"]]
                if not isinstance(directions, list) or any(
                    not isinstance(code, str) for code in directions
                ):
                    directions = []
                unique_directions = list(dict.fromkeys(directions))
                tag_by_code = {
                    tag.code: tag for tag in Tag.objects.filter(code__in=unique_directions)
                }
                tags = [tag_by_code[code] for code in unique_directions if code in tag_by_code]
                if not tags or len(tags) != len(unique_directions):
                    raise InputError({"direction": ["Выберите доступное направление."]})
                snapshot = _snapshot(
                    {
                        "name": draft.values.get("name"),
                        "contacts": draft.values.get("contacts"),
                        "request": draft.values.get("request"),
                        "tag_ids": [tag.pk for tag in tags],
                    }
                )
            contacts = _validate_fields(snapshot)
            tags = list(
                Tag.objects.select_for_update()
                .filter(
                    pk__in=snapshot["tag_ids"],
                )
                .order_by("pk")
            )
            if len(tags) != len(snapshot["tag_ids"]):
                raise InputError({"tag_ids": ["Выберите существующие теги."]})
            lead = Lead.objects.create(
                name=snapshot["name"],
                request=snapshot["request"],
                source=channel,
            )
            LeadContact.objects.bulk_create(
                [
                    LeadContact(
                        lead=lead, type=item.type, value=item.value, key=item.key, position=index
                    )
                    for index, item in enumerate(contacts)
                ]
            )
            lead.tags.set(tags)
            receipt = SubmissionReceipt.objects.create(
                submission_id=submission_id,
                channel=channel,
                owner_id=bot_user_id,
                payload=snapshot,
                lead=lead,
            )
            if draft:
                from leadflow.bot.models import DraftInput

                state.last_receipt = receipt
                state.last_submission_message_ids = list(
                    DraftInput.objects.filter(
                        draft=draft, source_message_id__isnull=False
                    ).values_list("source_message_id", flat=True)
                )
                state.save(update_fields=["last_receipt", "last_submission_message_ids"])
                draft.delete()
            return SubmissionResult(lead, False)
    except IntegrityError as error:
        # Only the UUID race is recoverable here. The atomic block has rolled back
        # before reading the winner; never query inside a broken transaction.
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) != (
            "crm_submissionreceipt_pkey"
        ):
            raise
        receipt = SubmissionReceipt.objects.select_related("lead").get(pk=submission_id)
        return _replay(receipt, channel, bot_user_id, snapshot)


def delete_tag(tag_id):
    with transaction.atomic():
        Tag.objects.select_for_update().get(pk=tag_id).delete()


def parse_submission_id(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError, TypeError, AttributeError:
        raise InputError({"submission_id": ["Неверный идентификатор заявки."]}) from None


def _snapshot(payload):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные заявки."]})
    unknown = payload.keys() - {"name", "contacts", "request", "tag_ids"}
    if unknown:
        raise InputError({key: ["Неизвестное поле."] for key in sorted(unknown)})
    tag_ids = payload.get("tag_ids", [])
    if (
        not isinstance(tag_ids, list)
        or any(type(value) is not int or value <= 0 for value in tag_ids)
        or len(set(tag_ids)) != len(tag_ids)
    ):
        raise InputError({"tag_ids": ["Передайте разные идентификаторы тегов."]})
    return deepcopy(
        {
            "name": payload.get("name"),
            "contacts": payload.get("contacts"),
            "request": payload.get("request"),
            "tag_ids": sorted(tag_ids),
        }
    )


def _validate_fields(payload):
    errors = {}
    for field, limit in [("name", 100), ("request", 2000)]:
        try:
            validate_text(payload[field], field, limit)
        except InputError as error:
            errors.update(error.field_errors)
    contacts = []
    try:
        contacts = validate_contacts(payload["contacts"])
    except InputError as error:
        errors.update(error.field_errors)
    if errors:
        raise InputError(errors)
    return contacts


def _replay(receipt, channel, owner_id, snapshot):
    if receipt.channel != channel or receipt.owner_id != owner_id:
        raise SubmissionForbidden("Submission belongs to another owner")
    if snapshot is not None and receipt.payload != snapshot:
        raise SubmissionConflict("Submission payload changed")
    return SubmissionResult(receipt.lead, True)
