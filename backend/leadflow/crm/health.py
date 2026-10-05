from django.db import DatabaseError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from leadflow.database import check_database


class HealthView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        try:
            check_database()
        except DatabaseError:
            return Response(
                {"status": "unavailable"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        return Response({"status": "ok"}, headers={"Cache-Control": "no-store"})
