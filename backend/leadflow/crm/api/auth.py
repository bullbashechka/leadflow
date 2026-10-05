from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import get_user_model
from django.contrib.auth import login
from django.contrib.auth import logout
from django.contrib.auth.hashers import check_password
from django.middleware.csrf import get_token
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from leadflow.crm.api.access import ACCESS_KEY
from leadflow.crm.api.access import DEMO_USERNAME
from leadflow.crm.api.access import EXPIRY_KEY
from leadflow.crm.api.access import MODE_KEY
from leadflow.crm.api.access import SESSION_DURATION
from leadflow.crm.api.access import VERSION_KEY
from leadflow.crm.api.access import SessionIdentity
from leadflow.crm.api.access import access_expiry
from leadflow.crm.api.access import crm_auth_mode
from leadflow.crm.api.access import enforce_csrf
from leadflow.crm.api.access import password_version
from leadflow.crm.api.access import record_login_attempt
from leadflow.crm.api.access import validated_hash
from leadflow.crm.api.errors import APIError


def session_response(request):
    expires = access_expiry(request)
    return Response(
        {
            "authenticated": expires is not None,
            "auth_mode": crm_auth_mode(),
            "expires_at": expires.isoformat() if expires else None,
            "server_time": timezone.now().isoformat(),
            "csrf_token": get_token(request._request),
        }
    )


class SessionView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [SessionIdentity]

    def get(self, request):
        return session_response(request)


class LoginView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [SessionIdentity]

    def post(self, request):
        enforce_csrf(request)
        mode = crm_auth_mode()
        data = request.data
        required = {"username", "password"} if mode == "individual" else {"password"}
        if (
            not isinstance(data, dict)
            or set(data) != required
            or not isinstance(data["password"], str)
            or not data["password"]
            or len(data["password"]) > 1024
        ):
            raise APIError(
                "validation_error",
                "Введите пароль.",
                field_errors={"password": ["Введите пароль."]},
            )
        if mode == "individual" and (
            not isinstance(data["username"], str)
            or not data["username"].strip()
            or len(data["username"]) > 150
        ):
            raise APIError(
                "validation_error",
                "Введите имя пользователя.",
                field_errors={"username": ["Введите имя пользователя."]},
            )
        if access_expiry(request) is not None:
            return session_response(request)
        encoded = validated_hash(settings.CRM_DEMO_PASSWORD_HASH) if mode == "demo" else None
        if mode == "demo" and encoded is None:
            raise APIError("configuration_error", "Вход временно недоступен.", status=503)
        record_login_attempt(request)
        if mode == "individual":
            user = authenticate(
                request._request, username=data["username"].strip(), password=data["password"]
            )
            if (
                user is None
                or user.is_staff
                or user.is_superuser
                or not user.has_usable_password()
                or user.username == DEMO_USERNAME
            ):
                raise APIError(
                    "invalid_password", "Неверное имя пользователя или пароль.", status=401
                )
            encoded = user.password
        else:
            if not check_password(data["password"], encoded):
                raise APIError("invalid_password", "Неверный пароль.", status=401)
            user = (
                get_user_model()
                .objects.filter(
                    username=DEMO_USERNAME, is_active=True, is_staff=False, is_superuser=False
                )
                .first()
            )
            if user is None or user.has_usable_password():
                raise APIError("configuration_error", "Вход временно недоступен.", status=503)
        login(request._request, user, backend="django.contrib.auth.backends.ModelBackend")
        expires = timezone.now() + SESSION_DURATION
        request.session[ACCESS_KEY] = True
        request.session[EXPIRY_KEY] = expires.isoformat()
        request.session[VERSION_KEY] = password_version(encoded)
        request.session[MODE_KEY] = mode
        request.session.set_expiry(expires)
        return session_response(request)


class LogoutView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = [SessionIdentity]

    def post(self, request):
        enforce_csrf(request)
        if request.data != {}:
            raise APIError("validation_error", "Некорректный запрос выхода.")
        logout(request._request)
        return Response(status=204)
