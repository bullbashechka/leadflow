from io import StringIO

import pytest
from django.core.management import call_command

from leadflow.crm.models import CRMState
from leadflow.crm.models import Lead


@pytest.mark.django_db
def test_demo_leads_seed_once_and_are_not_restored_after_deletion():
    output = StringIO()

    call_command("seed_demo_leads", stdout=output)
    original_ids = set(Lead.objects.values_list("pk", flat=True))
    call_command("seed_demo_leads", stdout=StringIO())
    Lead.objects.filter(pk__in=original_ids, is_demo=True).exclude(
        tags__isnull=True
    ).first().delete()
    call_command("seed_demo_leads", stdout=StringIO())

    leads = list(Lead.objects.filter(is_demo=True).prefetch_related("tags", "contacts"))
    state = CRMState.objects.get(pk=1)
    assert len(original_ids) == 6
    assert len(leads) == 5
    assert {lead.source for lead in leads} == {Lead.Source.MANUAL, Lead.Source.TELEGRAM_BOT}
    assert {lead.status for lead in leads} == set(Lead.Status.values)
    assert any(not lead.tags.exists() for lead in leads)
    assert all(lead.contacts.filter(value__endswith="@example.com").exists() for lead in leads)
    assert state.demo_seeded_at is not None
    assert "Created 6 fictional" in output.getvalue()
