"""Synchronous transactional domain operations, shared by HTTP and bot callers."""

import json
from copy import deepcopy
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from django.db import IntegrityError
from django.db import transaction
from django.db.models import F

from .models import CRMState
from .models import Lead
from .models import LeadContact
from .models import MutationReceipt
from .models import SubmissionReceipt
from .models import Tag
from .validation import MAX_SAFE_INTEGER
from .validation import InputError
from .validation import validate_contacts
from .validation import validate_text


class SubmissionConflict(ValueError):
    pass


class SubmissionForbidden(ValueError):
    pass


class StaleDraft(ValueError):
    pass


class OperationConflict(ValueError):
    pass


class VersionConflict(ValueError):
    def __init__(self, lead):
        self.lead = lead
        super().__init__("Lead changed since it was loaded")


class LeadDeleted(ValueError):
    pass


class LeadNotFound(ValueError):
    pass


class TagDeleted(ValueError):
    pass


class TagNotFound(ValueError):
    pass


class ProtectedTag(ValueError):
    pass


@dataclass(frozen=True)
class SubmissionResult:
    lead: Lead
    replayed: bool


def create_lead(submission_id, payload=None, *, bot_user_id=None, draft_revision=None, actor=None):
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
                return _replay(receipt, channel, bot_user_id, snapshot, actor)
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
                    return _replay(receipt, channel, bot_user_id, None, actor)
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
                arrival_sequence=_next_arrival_sequence(),
                created_by=actor,
                updated_by=actor,
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
                payload_hash=_fingerprint(snapshot),
                lead=lead,
                actor=actor,
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
        return _replay(receipt, channel, bot_user_id, snapshot, actor)


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
        or len(tag_ids) > 100
        or any(type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER for value in tag_ids)
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


def _replay(receipt, channel, owner_id, snapshot, actor=None):
    if receipt.channel != channel or receipt.owner_id != owner_id:
        raise SubmissionForbidden("Submission belongs to another owner")
    if receipt.actor_id is not None and receipt.actor_id != getattr(actor, "pk", None):
        raise SubmissionForbidden("Submission belongs to another actor")
    payload_hash = receipt.payload_hash or _fingerprint(receipt.payload)
    if snapshot is not None and payload_hash != _fingerprint(snapshot):
        raise SubmissionConflict("Submission payload changed")
    if receipt.lead is None:
        raise LeadDeleted("Submitted lead has been deleted")
    return SubmissionResult(receipt.lead, True)


@dataclass(frozen=True)
class LeadMutationResult:
    lead: Lead
    replayed: bool
    applied_version: int


@dataclass(frozen=True)
class TagMutationResult:
    tag: Tag | None
    tag_id: int
    replayed: bool
    created: bool
    affected_leads: int = 0


@dataclass(frozen=True)
class LeadDeleteResult:
    lead_id: UUID
    replayed: bool


def _next_arrival_sequence():
    CRMState.objects.get_or_create(pk=1)
    state = CRMState.objects.select_for_update().get(pk=1)
    state.last_arrival_sequence += 1
    state.save(update_fields=["last_arrival_sequence"])
    return state.last_arrival_sequence


def _fingerprint(value):
    serialized = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return sha256(serialized.encode("utf-8")).hexdigest()


def _parse_mutation_id(value):
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except ValueError, TypeError, AttributeError:
        raise InputError({"operation_id": ["Неверный идентификатор операции."]}) from None


def _expected_version(value):
    if type(value) is not int or not 1 <= value <= MAX_SAFE_INTEGER:
        raise InputError({"expected_version": ["Передайте актуальную версию заявки."]})
    return value


def _mutation_hash(kind, target_id, expected_version, payload):
    normalized_payload = deepcopy(payload)
    if isinstance(normalized_payload, dict) and isinstance(normalized_payload.get("tag_ids"), list):
        tag_ids = normalized_payload["tag_ids"]
        if all(type(tag_id) is int for tag_id in tag_ids):
            normalized_payload["tag_ids"] = sorted(tag_ids)
    return _fingerprint(
        {
            "kind": kind,
            "target_id": str(target_id),
            "expected_version": expected_version,
            "payload": normalized_payload,
        }
    )


def _find_mutation_replay(operation_id, kind, target_id, request_hash, actor=None):
    receipt = MutationReceipt.objects.filter(pk=operation_id).first()
    if receipt is None:
        return None
    if (
        receipt.kind != kind
        or receipt.target_id != str(target_id)
        or receipt.request_hash != request_hash
        or (receipt.actor_id is not None and receipt.actor_id != getattr(actor, "pk", None))
    ):
        raise OperationConflict("Mutation ID was already used")
    if kind in {MutationReceipt.Kind.UPDATE_LEAD, MutationReceipt.Kind.CHANGE_STATUS}:
        lead = Lead.objects.filter(pk=target_id).first()
        if lead is None:
            raise LeadDeleted("Lead has been deleted")
        return LeadMutationResult(lead, True, receipt.applied_version)
    if kind == MutationReceipt.Kind.CREATE_TAG:
        tag = Tag.objects.filter(pk=receipt.result.get("tag_id")).first()
        if tag is None:
            raise TagDeleted("Tag has been deleted")
        return TagMutationResult(tag, tag.pk, True, receipt.result.get("created", True))
    if kind == MutationReceipt.Kind.DELETE_TAG:
        return TagMutationResult(
            None, int(target_id), True, False, receipt.result.get("affected_leads", 0)
        )
    if kind == MutationReceipt.Kind.DELETE_LEAD:
        lead_id = target_id if isinstance(target_id, UUID) else UUID(str(target_id))
        return LeadDeleteResult(lead_id, True)
    return receipt


def _missing_lead_error(lead_id):
    known = SubmissionReceipt.objects.filter(deleted_lead_id=lead_id).exists() or (
        MutationReceipt.objects.filter(
            kind=MutationReceipt.Kind.DELETE_LEAD,
            target_id=str(lead_id),
        ).exists()
    )
    if known:
        return LeadDeleted("Lead has been deleted")
    return LeadNotFound("Lead does not exist")


def _validated_mutation(payload, *, allow_status=False):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные изменения."]})
    allowed = {"name", "contacts", "request", "note", "tag_ids"}
    if allow_status:
        allowed = {"status"}
    unknown = payload.keys() - allowed
    missing = allowed - payload.keys()
    errors = {key: ["Неизвестное поле."] for key in sorted(unknown)}
    errors.update({key: ["Поле обязательно."] for key in sorted(missing)})
    if errors:
        raise InputError(errors)
    if allow_status:
        status = payload["status"]
        if status not in Lead.Status.values:
            raise InputError({"status": ["Выберите допустимый статус."]})
        return {"status": status}, None, None

    snapshot = _snapshot({key: payload[key] for key in ("name", "contacts", "request", "tag_ids")})
    errors = {}
    try:
        contacts = _validate_fields(snapshot)
    except InputError as error:
        errors.update(error.field_errors)
        contacts = []
    note = payload["note"]
    if not isinstance(note, str):
        errors["note"] = ["Введите текст заметки."]
    elif "\x00" in note:
        errors["note"] = ["Удалите недопустимый нулевой символ."]
    elif len(note) > 5000:
        errors["note"] = ["Не более 5000 символов."]
    if errors:
        raise InputError(errors)
    return snapshot, contacts, note


def _store_mutation(
    operation_id, kind, target_id, request_hash, applied_version=None, *, result=None, actor=None
):
    return MutationReceipt.objects.create(
        operation_id=operation_id,
        kind=kind,
        target_id=str(target_id),
        request_hash=request_hash,
        applied_version=applied_version,
        result=result or {},
        actor=actor,
    )


def create_tag(payload, *, actor=None):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные тега."]})
    errors = {}
    unknown = payload.keys() - {"operation_id", "name"}
    if unknown:
        errors.update({key: ["Неизвестное поле."] for key in unknown})
    operation_id = _parse_mutation_id(payload.get("operation_id"))
    name = payload.get("name")
    if not isinstance(name, str):
        errors["name"] = ["Введите название тега."]
        clean_name = ""
    else:
        clean_name = name.strip()
        if not clean_name:
            errors["name"] = ["Введите название тега."]
        elif len(clean_name) > 40:
            errors["name"] = ["Не более 40 символов."]
        elif "\x00" in clean_name:
            errors["name"] = ["Удалите недопустимый нулевой символ."]
    if errors:
        raise InputError(errors)

    kind = MutationReceipt.Kind.CREATE_TAG
    request_hash = _mutation_hash(kind, "new", None, {"name": clean_name})
    try:
        with transaction.atomic():
            replay = _find_mutation_replay(operation_id, kind, "new", request_hash, actor)
            if replay:
                return replay
            existing = Tag.objects.select_for_update().filter(name__iexact=clean_name).first()
            if existing:
                _store_mutation(
                    operation_id,
                    kind,
                    "new",
                    request_hash,
                    result={"tag_id": existing.pk, "created": False},
                    actor=actor,
                )
                return TagMutationResult(existing, existing.pk, False, False)
            tag = Tag.objects.create(name=clean_name)
            _store_mutation(
                operation_id,
                kind,
                "new",
                request_hash,
                result={"tag_id": tag.pk, "created": True},
                actor=actor,
            )
            return TagMutationResult(tag, tag.pk, False, True)
    except IntegrityError as error:
        constraint = getattr(getattr(error.__cause__, "diag", None), "constraint_name", None)
        if constraint == "crm_tag_name_ci_unique":
            with transaction.atomic():
                replay = _find_mutation_replay(operation_id, kind, "new", request_hash, actor)
                if replay:
                    return replay
                existing = Tag.objects.select_for_update().get(name__iexact=clean_name)
                _store_mutation(
                    operation_id,
                    kind,
                    "new",
                    request_hash,
                    result={"tag_id": existing.pk, "created": False},
                    actor=actor,
                )
                return TagMutationResult(existing, existing.pk, False, False)
        if constraint == "crm_mutationreceipt_pkey":
            return _find_mutation_replay(operation_id, kind, "new", request_hash, actor)
        raise


def delete_tag(tag_id, payload, *, actor=None):
    if not isinstance(payload, dict) or payload.keys() != {"operation_id"}:
        raise InputError({"form": ["Передайте идентификатор операции."]})
    operation_id = _parse_mutation_id(payload.get("operation_id"))
    kind = MutationReceipt.Kind.DELETE_TAG
    request_hash = _mutation_hash(kind, tag_id, None, {})
    with transaction.atomic():
        replay = _find_mutation_replay(operation_id, kind, tag_id, request_hash, actor)
        if replay:
            return replay
        tag = Tag.objects.select_for_update().filter(pk=tag_id).first()
        if tag is None:
            replay = _find_mutation_replay(operation_id, kind, tag_id, request_hash, actor)
            if replay:
                return replay
            if MutationReceipt.objects.filter(kind=kind, target_id=str(tag_id)).exists():
                raise TagDeleted("Tag was deleted by another operation")
            raise TagNotFound("Tag does not exist")
        if tag.is_system:
            raise ProtectedTag("System tags cannot be deleted")
        affected_lead_ids = list(
            Lead.objects.select_for_update()
            .filter(tags__pk=tag.pk)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        affected = len(affected_lead_ids)
        if affected_lead_ids:
            Lead.objects.filter(pk__in=affected_lead_ids).update(
                version=F("version") + 1, updated_by=actor
            )
        _store_mutation(
            operation_id,
            kind,
            tag_id,
            request_hash,
            result={"affected_leads": affected},
            actor=actor,
        )
        tag.delete()
        return TagMutationResult(None, tag_id, False, False, affected)


def delete_lead(lead_id, payload, *, actor=None):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные удаления."]})
    errors = {}
    if payload.keys() - {"operation_id", "expected_version"}:
        errors["form"] = ["Переданы лишние поля."]
    operation_id = _parse_mutation_id(payload.get("operation_id"))
    expected_version = _expected_version(payload.get("expected_version"))
    if errors:
        raise InputError(errors)
    kind = MutationReceipt.Kind.DELETE_LEAD
    request_hash = _mutation_hash(kind, lead_id, expected_version, {})
    try:
        with transaction.atomic():
            replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
            if replay:
                return replay
            try:
                lead = Lead.objects.select_for_update().get(pk=lead_id)
            except Lead.DoesNotExist:
                replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
                if replay:
                    return replay
                raise _missing_lead_error(lead_id) from None
            replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
            if replay:
                return replay
            if lead.version != expected_version:
                raise VersionConflict(lead)
            SubmissionReceipt.objects.filter(lead=lead).update(
                lead=None, deleted_lead_id=lead.pk, payload={}
            )
            _store_mutation(operation_id, kind, lead_id, request_hash, result={}, actor=actor)
            lead.delete()
            return LeadDeleteResult(lead_id, False)
    except IntegrityError as error:
        if getattr(getattr(error.__cause__, "diag", None), "constraint_name", None) == (
            "crm_mutationreceipt_pkey"
        ):
            return _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
        raise


def update_lead(lead_id, payload, *, actor=None):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные изменения."]})
    operation_id = _parse_mutation_id(payload.get("operation_id"))
    expected_version = _expected_version(payload.get("expected_version"))
    values = {
        key: value
        for key, value in payload.items()
        if key not in {"operation_id", "expected_version"}
    }
    kind = MutationReceipt.Kind.UPDATE_LEAD
    request_hash = _mutation_hash(kind, lead_id, expected_version, values)
    with transaction.atomic():
        replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
        if replay:
            return replay
        snapshot, contacts, note = _validated_mutation(values)
        tags = list(
            Tag.objects.select_for_update().filter(pk__in=snapshot["tag_ids"]).order_by("pk")
        )
        try:
            lead = Lead.objects.select_for_update().get(pk=lead_id)
        except Lead.DoesNotExist:
            raise _missing_lead_error(lead_id) from None
        replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
        if replay:
            return replay
        if lead.version != expected_version:
            raise VersionConflict(lead)
        if len(tags) != len(snapshot["tag_ids"]):
            raise InputError({"tag_ids": ["Тег удалён или недоступен."]})
        current_contacts = list(lead.contacts.order_by("position").values_list("value", flat=True))
        current_tag_ids = sorted(lead.tags.values_list("pk", flat=True))
        changed = (
            lead.name != snapshot["name"]
            or current_contacts != [contact.value for contact in contacts]
            or lead.request != snapshot["request"]
            or lead.note != note
            or current_tag_ids != snapshot["tag_ids"]
        )
        if changed:
            lead.name = snapshot["name"]
            lead.request = snapshot["request"]
            lead.note = note
            lead.version += 1
            lead.updated_by = actor
            lead.save(update_fields=["name", "request", "note", "version", "updated_by"])
            lead.contacts.all().delete()
            LeadContact.objects.bulk_create(
                [
                    LeadContact(
                        lead=lead,
                        type=contact.type,
                        value=contact.value,
                        key=contact.key,
                        position=index,
                    )
                    for index, contact in enumerate(contacts)
                ]
            )
            lead.tags.set(tags)
        _store_mutation(operation_id, kind, lead_id, request_hash, lead.version, actor=actor)
        return LeadMutationResult(lead, False, lead.version)


def change_lead_status(lead_id, payload, *, actor=None):
    if not isinstance(payload, dict):
        raise InputError({"form": ["Передайте данные изменения."]})
    operation_id = _parse_mutation_id(payload.get("operation_id"))
    expected_version = _expected_version(payload.get("expected_version"))
    values = {
        key: value
        for key, value in payload.items()
        if key not in {"operation_id", "expected_version"}
    }
    kind = MutationReceipt.Kind.CHANGE_STATUS
    request_hash = _mutation_hash(kind, lead_id, expected_version, values)
    with transaction.atomic():
        replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
        if replay:
            return replay
        cleaned, _, _ = _validated_mutation(values, allow_status=True)
        try:
            lead = Lead.objects.select_for_update().get(pk=lead_id)
        except Lead.DoesNotExist:
            raise _missing_lead_error(lead_id) from None
        replay = _find_mutation_replay(operation_id, kind, lead_id, request_hash, actor)
        if replay:
            return replay
        if lead.version != expected_version:
            raise VersionConflict(lead)
        if lead.status != cleaned["status"]:
            lead.status = cleaned["status"]
            lead.version += 1
            lead.updated_by = actor
            lead.save(update_fields=["status", "version", "updated_by"])
        _store_mutation(operation_id, kind, lead_id, request_hash, lead.version, actor=actor)
        return LeadMutationResult(lead, False, lead.version)
