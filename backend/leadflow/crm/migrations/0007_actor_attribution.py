from django.conf import settings
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [
        ("crm", "0006_mutationreceipt_result"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="lead",
            name="created_by",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                on_delete=models.SET_NULL,
                null=True,
                blank=True,
                related_name="created_leads",
            ),
        ),
        migrations.AddField(
            model_name="lead",
            name="updated_by",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                on_delete=models.SET_NULL,
                null=True,
                blank=True,
                related_name="updated_leads",
            ),
        ),
        migrations.AddField(
            model_name="submissionreceipt",
            name="actor",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                on_delete=models.PROTECT,
                null=True,
                blank=True,
                related_name="submission_receipts",
            ),
        ),
        migrations.AddField(
            model_name="mutationreceipt",
            name="actor",
            field=models.ForeignKey(
                settings.AUTH_USER_MODEL,
                on_delete=models.PROTECT,
                null=True,
                blank=True,
                related_name="mutation_receipts",
            ),
        ),
    ]
