"""Protected read and manual-create endpoints for the CRM."""

import re
from urllib.parse import urlencode
from uuid import UUID

from django.db.models import Count
from django.db.models import Exists
from django.db.models import OuterRef
from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from leadflow.crm.api.errors import APIError
from leadflow.crm.models import CRMState
from leadflow.crm.models import Lead
from leadflow.crm.models import LeadContact
from leadflow.crm.models import Tag
from leadflow.crm.services import LeadDeleted
from leadflow.crm.services import LeadNotFound
from leadflow.crm.services import OperationConflict
from leadflow.crm.services import ProtectedTag
from leadflow.crm.services import SubmissionConflict
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.services import TagDeleted
from leadflow.crm.services import TagNotFound
from leadflow.crm.services import VersionConflict
from leadflow.crm.services import change_lead_status
from leadflow.crm.services import create_lead
from leadflow.crm.services import create_tag
from leadflow.crm.services import delete_lead
from leadflow.crm.services import delete_tag
from leadflow.crm.services import update_lead
from leadflow.crm.validation import InputError


def serialize_lead(lead):
    return {
        "id": str(lead.pk),
        "name": lead.name,
        "contacts": [
            {"type": contact.type, "value": contact.value} for contact in lead.contacts.all()
        ],
        "request": lead.request,
        "note": lead.note,
        "source": lead.source,
        "status": lead.status,
        "version": lead.version,
        "is_demo": lead.is_demo,
        "arrival_sequence": lead.arrival_sequence,
        "created_at": lead.created_at.isoformat().replace("+00:00", "Z"),
        "tags": [
            {"id": tag.pk, "name": tag.name, "is_system": tag.is_system} for tag in lead.tags.all()
        ],
    }


def _serialize_tag(tag):
    if tag is None:
        return None
    return {"id": tag.pk, "name": tag.name, "is_system": tag.is_system}


def _one_parameter(params, name, errors):
    values = params.getlist(name)
    if len(values) > 1:
        errors[name] = ["Передайте параметр один раз."]
        return None
    return values[0] if values else None


def _integer_parameter(value, name, *, default, minimum, maximum=None):
    if value is None:
        return default
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(name)
    parsed = int(value)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ValueError(name)
    return parsed


def _list_options(request):
    params = request.query_params
    allowed = {
        "tag_id",
        "limit",
        "offset",
        "before_id",
        "before_sequence",
        "q",
        "status",
        "since_sequence",
        "exclude_id",
    }
    errors = {
        name: ["Параметр не поддерживается."] for name in params.keys() if name not in allowed
    }
    repeatable = {"exclude_id"}
    values = {
        name: params.getlist(name) if name in repeatable else _one_parameter(params, name, errors)
        for name in allowed
    }
    if errors:
        raise APIError("validation_error", "Проверьте параметры списка.", field_errors=errors)

    try:
        limit = _integer_parameter(values["limit"], "limit", default=50, minimum=1, maximum=100)
        offset = _integer_parameter(values["offset"], "offset", default=0, minimum=0)
    except ValueError as error:
        field = str(error)
        raise APIError(
            "validation_error",
            "Проверьте параметры списка.",
            field_errors={field: ["Укажите допустимое целое число."]},
        ) from None

    tag_id = None
    if values["tag_id"] is not None:
        try:
            tag_id = _integer_parameter(values["tag_id"], "tag_id", default=None, minimum=1)
        except ValueError:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"tag_id": ["Выберите существующий тег."]},
            ) from None
        if not Tag.objects.filter(pk=tag_id).exists():
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"tag_id": ["Выберите существующий тег."]},
            )

    anchor_id = None
    if values["before_id"] is not None:
        if values["offset"] is not None and offset != 0:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"before_id": ["Не сочетайте с ненулевым offset."]},
            )
        try:
            anchor_id = UUID(values["before_id"])
        except ValueError:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"before_id": ["Укажите идентификатор существующего лида."]},
            ) from None

    before_sequence = None
    if values["before_sequence"] is not None:
        try:
            before_sequence = _integer_parameter(
                values["before_sequence"], "before_sequence", default=None, minimum=1
            )
        except ValueError:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"before_sequence": ["Укажите допустимый курсор."]},
            ) from None
        if anchor_id is not None:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"before_sequence": ["Используйте только один курсор."]},
            )
    status = values["status"]
    if status is not None and status not in Lead.Status.values:
        raise APIError(
            "validation_error",
            "Проверьте параметры списка.",
            field_errors={"status": ["Выберите существующий статус."]},
        )
    query = values["q"]
    if query is not None and len(query) > 200:
        raise APIError(
            "validation_error",
            "Проверьте параметры списка.",
            field_errors={"q": ["Не более 200 символов."]},
        )
    since_sequence = None
    if values["since_sequence"] is not None:
        try:
            since_sequence = _integer_parameter(
                values["since_sequence"], "since_sequence", default=None, minimum=0
            )
        except ValueError:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"since_sequence": ["Укажите допустимый курсор."]},
            ) from None
    exclude_ids = []
    for value in values["exclude_id"]:
        try:
            exclude_ids.append(UUID(value))
        except ValueError:
            raise APIError(
                "validation_error",
                "Проверьте параметры списка.",
                field_errors={"exclude_id": ["Укажите допустимый идентификатор."]},
            ) from None
    if len(exclude_ids) > 100:
        raise APIError(
            "validation_error",
            "Проверьте параметры списка.",
            field_errors={"exclude_id": ["Передайте не более 100 идентификаторов."]},
        )
    return (
        tag_id,
        limit,
        offset,
        anchor_id,
        before_sequence,
        query,
        status,
        since_sequence,
        exclude_ids,
    )


def _relative_url(request, **values):
    return f"{request.path}?{urlencode(values)}"


def _create_manual_lead(request):
    body = request.data
    if isinstance(body, dict):
        payload = dict(body)
        submission_id = payload.pop("submission_id", None)
    else:
        payload = body
        submission_id = None
    try:
        result = create_lead(submission_id, payload, actor=request.user)
    except InputError as error:
        raise APIError(
            "validation_error",
            "Проверьте заполнение формы.",
            field_errors=error.field_errors,
        ) from None
    except SubmissionConflict:
        raise APIError(
            "submission_conflict",
            "Идентификатор уже использован для других данных.",
            status=409,
        ) from None
    except SubmissionForbidden:
        raise APIError("permission_denied", "Действие недоступно.", status=403) from None
    except LeadDeleted:
        raise APIError(
            "submission_deleted",
            "Эта заявка уже была сохранена и затем удалена.",
            status=410,
        ) from None

    return Response(
        serialize_lead(result.lead),
        status=200 if result.replayed else 201,
    )


class TagListView(APIView):
    def get(self, request):
        tags = Tag.objects.order_by("pk").annotate(lead_count=Count("leads", distinct=True))
        return Response(
            {
                "results": [
                    {
                        **_serialize_tag(tag),
                        "lead_count": tag.lead_count,
                    }
                    for tag in tags
                ]
            }
        )

    def post(self, request):
        try:
            result = create_tag(request.data, actor=request.user)
        except InputError as error:
            raise APIError(
                "validation_error", "Проверьте название тега.", field_errors=error.field_errors
            ) from None
        except OperationConflict:
            raise APIError(
                "operation_conflict",
                "Идентификатор операции уже использован для других данных.",
                status=409,
            ) from None
        except TagDeleted:
            raise APIError("tag_deleted", "Тег уже удалён.", status=410) from None
        return Response(
            {
                "tag": _serialize_tag(result.tag),
                "created": result.created,
                "replayed": result.replayed,
            },
            status=201 if result.created and not result.replayed else 200,
        )


class TagDetailView(APIView):
    def delete(self, request, tag_id):
        try:
            result = delete_tag(tag_id, request.data, actor=request.user)
        except InputError as error:
            raise APIError(
                "validation_error", "Проверьте операцию.", field_errors=error.field_errors
            ) from None
        except OperationConflict:
            raise APIError(
                "operation_conflict",
                "Идентификатор операции уже использован для других данных.",
                status=409,
            ) from None
        except ProtectedTag:
            raise APIError(
                "protected_tag", "Исходное направление нельзя удалить.", status=409
            ) from None
        except TagDeleted:
            raise APIError("tag_deleted", "Тег уже удалён.", status=410) from None
        except TagNotFound:
            raise APIError("not_found", "Тег не найден.", status=404) from None
        return Response(
            {
                "tag_id": result.tag_id,
                "deleted": True,
                "replayed": result.replayed,
                "affected_leads": result.affected_leads,
            }
        )


class LeadListView(APIView):
    def post(self, request):
        return _create_manual_lead(request)

    def get(self, request):
        (
            tag_id,
            limit,
            offset,
            anchor_id,
            before_sequence,
            query,
            status,
            since_sequence,
            exclude_ids,
        ) = _list_options(request)
        # Capture the committed boundary first; every count and row uses that same bound.
        latest_sequence = CRMState.objects.get(pk=1).last_arrival_sequence
        leads = Lead.objects.filter(arrival_sequence__lte=latest_sequence)
        if tag_id is not None:
            leads = leads.filter(tags__pk=tag_id)
        if status:
            leads = leads.filter(status=status)
        terms = list(dict.fromkeys(query.split())) if query else []
        for term in terms:
            digits = re.sub(r"\D", "", term)
            contact_clause = Q(value__icontains=term) | Q(key__icontains=term)
            if digits:
                contact_clause |= Q(key__icontains=digits)
            contacts = LeadContact.objects.filter(lead_id=OuterRef("pk")).filter(contact_clause)
            leads = leads.filter(
                Q(name__icontains=term) | Q(request__icontains=term) | Exists(contacts)
            )
        leads = (
            leads.distinct()
            .order_by("-arrival_sequence", "-pk")
            .prefetch_related("contacts", "tags")
        )
        count = leads.count()
        page = leads

        if anchor_id is not None:
            anchor = leads.filter(pk=anchor_id).first()
            if anchor is None:
                raise APIError(
                    "validation_error",
                    "Проверьте параметры списка.",
                    field_errors={"before_id": ["Лид не найден в этом списке."]},
                )
            page = page.filter(arrival_sequence__lt=anchor.arrival_sequence)
        elif before_sequence is not None:
            page = page.filter(arrival_sequence__lt=before_sequence)

        new_count = 0
        if since_sequence is not None:
            recent = leads.filter(arrival_sequence__gt=since_sequence)
            if exclude_ids:
                recent = recent.exclude(pk__in=exclude_ids)
            new_count = recent.count()

        if anchor_id is not None or before_sequence is not None:
            fetched = list(page[: limit + 1])
            has_more = len(fetched) > limit
            results = fetched[:limit]
            next_url = None
            if has_more:
                query = {"limit": limit}
                if tag_id is not None:
                    query["tag_id"] = tag_id
                query["before_sequence"] = results[-1].arrival_sequence
                if status:
                    query["status"] = status
                if query_terms := terms:
                    query["q"] = " ".join(query_terms)
                next_url = _relative_url(request, **query)
            previous_url = None
        else:
            start = offset or 0
            fetched = list(page[start : start + limit + 1])
            has_more = len(fetched) > limit
            results = fetched[:limit]
            common = {"limit": limit}
            if tag_id is not None:
                common["tag_id"] = tag_id
            if status:
                common["status"] = status
            if terms:
                common["q"] = " ".join(terms)
            next_url = None
            if has_more:
                next_url = _relative_url(
                    request,
                    **common,
                    **(
                        {"offset": start + len(results)}
                        if offset
                        else {"before_sequence": results[-1].arrival_sequence}
                    ),
                )
            previous_url = None
            if offset:
                previous_url = _relative_url(request, **common, offset=max(0, offset - limit))

        return Response(
            {
                "count": count,
                "next": next_url,
                "previous": previous_url,
                "new_count": new_count,
                "latest_sequence": latest_sequence,
                "results": [serialize_lead(lead) for lead in results],
            }
        )


class LeadDetailView(APIView):
    def get(self, request, lead_id):
        try:
            lead = Lead.objects.prefetch_related("contacts", "tags").get(pk=lead_id)
        except Lead.DoesNotExist:
            raise APIError("not_found", "Заявка не найдена.", status=404) from None
        return Response(serialize_lead(lead))

    def put(self, request, lead_id):
        try:
            result = update_lead(lead_id, request.data, actor=request.user)
        except InputError as error:
            raise APIError(
                "validation_error",
                "Проверьте заполнение формы.",
                field_errors=error.field_errors,
            ) from None
        except VersionConflict as error:
            raise APIError(
                "version_conflict",
                "Заявка изменилась в другой вкладке. Сверьте данные и повторите сохранение.",
                status=409,
                current_lead=serialize_lead(error.lead),
            ) from None
        except OperationConflict:
            raise APIError(
                "operation_conflict",
                "Идентификатор операции уже использован для других данных.",
                status=409,
            ) from None
        except LeadDeleted:
            raise APIError(
                "lead_deleted", "Заявка уже удалена. Сохранение не восстановит её.", status=410
            ) from None
        except LeadNotFound:
            raise APIError("not_found", "Заявка не найдена.", status=404) from None
        return Response(_mutation_data(result, request.data["operation_id"]))

    def delete(self, request, lead_id):
        try:
            result = delete_lead(lead_id, request.data, actor=request.user)
        except InputError as error:
            raise APIError(
                "validation_error", "Проверьте операцию.", field_errors=error.field_errors
            ) from None
        except VersionConflict as error:
            raise APIError(
                "version_conflict",
                "Заявка изменилась в другой вкладке.",
                status=409,
                current_lead=serialize_lead(error.lead),
            ) from None
        except OperationConflict:
            raise APIError(
                "operation_conflict",
                "Идентификатор операции уже использован для других данных.",
                status=409,
            ) from None
        except LeadDeleted:
            raise APIError("lead_deleted", "Заявка уже удалена.", status=410) from None
        except LeadNotFound:
            raise APIError("not_found", "Заявка не найдена.", status=404) from None
        return Response(
            {"lead_id": str(result.lead_id), "deleted": True, "replayed": result.replayed}
        )


class LeadStatusView(APIView):
    def patch(self, request, lead_id):
        try:
            result = change_lead_status(lead_id, request.data, actor=request.user)
        except InputError as error:
            raise APIError(
                "validation_error",
                "Проверьте заполнение формы.",
                field_errors=error.field_errors,
            ) from None
        except VersionConflict as error:
            raise APIError(
                "version_conflict",
                "Статус заявки изменился в другой вкладке. Сверьте данные и повторите действие.",
                status=409,
                current_lead=serialize_lead(error.lead),
            ) from None
        except OperationConflict:
            raise APIError(
                "operation_conflict",
                "Идентификатор операции уже использован для других данных.",
                status=409,
            ) from None
        except LeadDeleted:
            raise APIError("lead_deleted", "Заявка уже удалена.", status=410) from None
        except LeadNotFound:
            raise APIError("not_found", "Заявка не найдена.", status=404) from None
        return Response(_mutation_data(result, request.data["operation_id"]))


def _mutation_data(result, operation_id):
    return {
        "operation_id": str(operation_id),
        "replayed": result.replayed,
        "applied_version": result.applied_version,
        "lead": serialize_lead(result.lead),
    }
