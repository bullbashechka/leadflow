"""Protected read and manual-create endpoints for the CRM."""

import re
from urllib.parse import urlencode
from uuid import UUID

from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

from leadflow.crm.api.errors import APIError
from leadflow.crm.models import Lead
from leadflow.crm.models import Tag
from leadflow.crm.services import SubmissionConflict
from leadflow.crm.services import SubmissionForbidden
from leadflow.crm.services import create_lead
from leadflow.crm.validation import InputError


def serialize_lead(lead):
    return {
        "id": str(lead.pk),
        "name": lead.name,
        "contacts": [
            {"type": contact.type, "value": contact.value} for contact in lead.contacts.all()
        ],
        "request": lead.request,
        "source": lead.source,
        "status": lead.status,
        "created_at": lead.created_at.isoformat().replace("+00:00", "Z"),
        "tags": [
            {"id": tag.pk, "name": tag.name, "is_system": tag.is_system} for tag in lead.tags.all()
        ],
    }


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
    allowed = {"tag_id", "limit", "offset", "before_id"}
    errors = {
        name: ["Параметр не поддерживается."] for name in params.keys() if name not in allowed
    }
    values = {name: _one_parameter(params, name, errors) for name in allowed}
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

    return tag_id, limit, offset, anchor_id


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
        result = create_lead(submission_id, payload)
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

    return Response(
        serialize_lead(result.lead),
        status=200 if result.replayed else 201,
    )


class TagListView(APIView):
    def get(self, request):
        tags = Tag.objects.order_by("pk")
        return Response(
            {
                "results": [
                    {"id": tag.pk, "name": tag.name, "is_system": tag.is_system} for tag in tags
                ]
            }
        )


class LeadListView(APIView):
    def post(self, request):
        return _create_manual_lead(request)

    def get(self, request):
        tag_id, limit, offset, anchor_id = _list_options(request)
        leads = Lead.objects.all()
        if tag_id is not None:
            leads = leads.filter(tags__pk=tag_id)
        leads = leads.order_by("-created_at", "-pk").prefetch_related("contacts", "tags")
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
            page = page.filter(
                Q(created_at__lt=anchor.created_at)
                | Q(created_at=anchor.created_at, pk__lt=anchor.pk)
            )
            fetched = list(page[: limit + 1])
            has_more = len(fetched) > limit
            results = fetched[:limit]
            next_url = None
            if has_more:
                query = {"limit": limit}
                if tag_id is not None:
                    query["tag_id"] = tag_id
                query["before_id"] = results[-1].pk
                next_url = _relative_url(request, **query)
            previous_url = None
        else:
            fetched = list(page[offset : offset + limit + 1])
            has_more = len(fetched) > limit
            results = fetched[:limit]
            common = {"limit": limit}
            if tag_id is not None:
                common["tag_id"] = tag_id
            next_url = None
            if has_more:
                next_url = _relative_url(
                    request,
                    **common,
                    offset=offset + len(results),
                )
            previous_url = None
            if offset:
                previous_url = _relative_url(request, **common, offset=max(0, offset - limit))

        return Response(
            {
                "count": count,
                "next": next_url,
                "previous": previous_url,
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
