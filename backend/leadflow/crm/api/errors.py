from django.db import DatabaseError
from django.http import JsonResponse
from django.views.csrf import csrf_failure as default_csrf_failure
from rest_framework.exceptions import APIException
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.exceptions import NotAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response


def envelope(code, message, field_errors=None):
    return {"code": code, "message": message, "field_errors": field_errors or {}}


class APIError(APIException):
    def __init__(self, code, message, status=400, field_errors=None, retry_after=None):
        super().__init__(message, code=code)
        self.status_code = status
        self.payload = envelope(code, message, field_errors)
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
        return response
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
            response["Cache-Control"] = "no-store"
        return response
