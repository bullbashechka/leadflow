"""Small shared database probe for HTTP and the standalone bot process."""

from django.db import connection


def check_database():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
