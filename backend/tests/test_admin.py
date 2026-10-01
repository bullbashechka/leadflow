import pytest
from django.urls import reverse


def test_admin_requires_login(client):
    response = client.get(reverse("admin:index"))
    assert response.status_code == 302
    assert response.url.startswith(reverse("admin:login"))


@pytest.mark.django_db
def test_admin_can_manage_technical_users(admin_client):
    for name in ["admin:index", "admin:users_user_changelist", "admin:users_user_add"]:
        assert admin_client.get(reverse(name)).status_code == 200
