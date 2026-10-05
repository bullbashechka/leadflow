from io import StringIO
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

pytestmark = pytest.mark.django_db
PASSWORD = "synthetic-operator-secret#9207"


def test_create_operator_reads_password_privately_and_grants_only_crm_role():
    output = StringIO()
    with patch(
        "leadflow.users.management.commands.create_crm_operator.getpass", return_value=PASSWORD
    ):
        call_command("create_crm_operator", "operator", stdout=output)
    user = get_user_model().objects.get(username="operator")
    assert user.is_active and not user.is_staff and not user.is_superuser
    assert user.check_password(PASSWORD)
    assert PASSWORD not in output.getvalue()


@pytest.mark.parametrize("values", [["short", "short"], [PASSWORD, "different"]])
def test_create_operator_rejects_weak_or_mismatched_password_without_saving(values):
    with patch(
        "leadflow.users.management.commands.create_crm_operator.getpass", side_effect=values
    ):
        with pytest.raises(CommandError):
            call_command("create_crm_operator", "operator")
    assert not get_user_model().objects.filter(username="operator").exists()


def test_create_operator_cannot_overwrite_existing_account():
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    with patch("leadflow.users.management.commands.create_crm_operator.getpass") as prompt:
        with pytest.raises(CommandError):
            call_command("create_crm_operator", "operator")
    prompt.assert_not_called()
    user.refresh_from_db()
    assert user.check_password(PASSWORD)


def test_revoke_operator_deactivates_without_deleting_account():
    user = get_user_model().objects.create_user(username="operator", password=PASSWORD)
    call_command("revoke_crm_operator", "operator")
    user.refresh_from_db()
    assert not user.is_active
    assert user.check_password(PASSWORD)


@pytest.mark.parametrize("flags", [{"is_staff": True}, {"is_superuser": True}])
def test_revoke_operator_does_not_modify_technical_administrators(flags):
    user = get_user_model().objects.create_user(username="technical", password=PASSWORD, **flags)
    with pytest.raises(CommandError):
        call_command("revoke_crm_operator", "technical")
    user.refresh_from_db()
    assert user.is_active
