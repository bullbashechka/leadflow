import logging

from django.db import DatabaseError
from django.http import JsonResponse
from django.views.csrf import csrf_failure as default_csrf_failure
from rest_framework.exceptions import APIException
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import NotAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

logger = logging.getLogger(__name__)


def envelope(code, message, field_errors=None):
    return {"code": code, "message": message, "field_errors": field_errors or {}}


class APIError(APIException):
    def __init__(
        self,
        code,
        message,
        status=400,
        field_errors=None,
        retry_after=None,
        current_lead=None,
        existing_tag=None,
    ):
        super().__init__(message, code=code)
        self.status_code = status
        self.payload = envelope(code, message, field_errors)
        if current_lead is not None:
            self.payload["current_lead"] = current_lead
        if existing_tag is not None:
            self.payload["existing_tag"] = existing_tag
        self.retry_after = retry_after


def exception_handler(exc, context):
    from rest_framework.views import exception_handler as drf_exception_handler

    if isinstance(exc, APIError):
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else {}
        return Response(exc.payload, status=exc.status_code, headers=headers)
    if isinstance(exc, DatabaseError):
        return Response(envelope("service_unavailable", "Сервер временно недоступен."), status=503)
    response = drf_exception_handler(exc, context)
    if response is None:
        # Keep traceback locations without logging exception text or chained errors,
        # which can contain credentials or customer data.
        safe_error = RuntimeError(f"Unhandled CRM API failure ({type(exc).__name__})")
        logger.error("%s", safe_error, exc_info=(type(safe_error), safe_error, exc.__traceback__))
        return Response(envelope("service_unavailable", "Сервер временно недоступен."), status=500)
    if isinstance(exc, AuthenticationFailed | NotAuthenticated):
        response.data = envelope("authentication_required", "Войдите в CRM.")
    elif isinstance(exc, PermissionDenied):
        response.data = envelope("permission_denied", "Действие недоступно.")
    else:
        code = "validation_error" if response.status_code == 400 else "request_error"
        response.data = envelope(code, "Не удалось выполнить запрос.")
    return response


def csrf_failure(request, reason=""):
    if request.path.startswith("/api/"):
        return JsonResponse(
            envelope("csrf_failed", "Обновите доступ и повторите действие."), status=403
        )
    return default_csrf_failure(request, reason=reason)


class APINoStoreMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/"):
            if response.status_code >= 400 and not response.get("Content-Type", "").startswith(
                "application/json"
            ):
                status = response.status_code
                code = (
                    "not_found"
                    if status == 404
                    else "service_unavailable"
                    if status >= 500
                    else "request_error"
                )
                message = "Ресурс не найден." if status == 404 else "Не удалось выполнить запрос."
                response = JsonResponse(envelope(code, message), status=status)
            response["Cache-Control"] = "no-store"
        return response
