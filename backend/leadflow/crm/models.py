import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.functions import Lower
from django.db.models.signals import pre_delete
from django.dispatch import receiver
from django.utils import timezone

SYSTEM_TAGS = {
    "website": "Сайт",
    "advertising": "Реклама",
    "automation": "Автоматизация",
    "other": "Другое",
}


class LoginAttempt(models.Model):
    """Atomic per-peer limit, shared by API workers without another service."""

    source_key = models.CharField(primary_key=True, max_length=64)
    started_at = models.DateTimeField()
    attempts = models.PositiveIntegerField(default=0)

    def __str__(self):
        return self.source_key


class Tag(models.Model):
    name = models.CharField(max_length=40)
    code = models.CharField(max_length=20, unique=True, null=True, blank=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="crm_tag_name_ci_unique"),
            models.CheckConstraint(
                condition=models.Q(code__isnull=True) | models.Q(code__in=list(SYSTEM_TAGS)),
                name="crm_tag_known_system_code",
            ),
        ]

    def __str__(self):
        return self.name

    @property
    def is_system(self):
        return self.code is not None


@receiver(pre_delete, sender=Tag)
def protect_system_tag(sender, instance, **kwargs):
    if instance.is_system:
        raise ValidationError("Исходный тег нельзя удалить.")


class Lead(models.Model):
    class Source(models.TextChoices):
        MANUAL = "manual", "Вручную"
        TELEGRAM_BOT = "telegram_bot", "Telegram-бот"

    class Status(models.TextChoices):
        NEW = "new", "Новый"
        IN_PROGRESS = "in_progress", "В работе"
        CLOSED = "closed", "Закрыт"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    request = models.TextField(max_length=2000)
    note = models.TextField(max_length=5000, blank=True, default="")
    source = models.CharField(max_length=20, choices=Source)
    status = models.CharField(max_length=20, choices=Status, default=Status.NEW)
    version = models.PositiveBigIntegerField(default=1)
    is_demo = models.BooleanField(default=False)
    arrival_sequence = models.PositiveBigIntegerField(null=True, blank=True, unique=True)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    tags = models.ManyToManyField(Tag, related_name="leads", blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_leads",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="updated_leads",
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(source__in=["manual", "telegram_bot"]),
                name="crm_lead_known_source",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["new", "in_progress", "closed"]),
                name="crm_lead_known_status",
            ),
        ]

    def __str__(self):
        return str(self.pk)


class LeadContact(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="contacts")
    type = models.CharField(
        max_length=10,
        choices=[
            ("phone", "Телефон"),
            ("email", "Email"),
            ("telegram", "Telegram"),
        ],
    )
    value = models.CharField(max_length=254)
    # Unicode case folding can expand a valid <=254-character email.
    key = models.TextField()
    position = models.PositiveIntegerField()

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(fields=["lead", "type", "key"], name="crm_contact_unique"),
            models.UniqueConstraint(
                fields=["lead", "position"], name="crm_contact_position_unique"
            ),
            models.CheckConstraint(
                condition=models.Q(type__in=["phone", "email", "telegram"]),
                name="crm_contact_known_type",
            ),
        ]

    def __str__(self):
        return f"{self.lead_id}:{self.position}"


class SubmissionReceipt(models.Model):
    submission_id = models.UUIDField(primary_key=True)
    channel = models.CharField(max_length=20, choices=Lead.Source)
    owner_id = models.BigIntegerField(null=True)
    payload = models.JSONField()
    deleted_lead_id = models.UUIDField(null=True, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True)
    lead = models.OneToOneField(
        Lead, on_delete=models.SET_NULL, related_name="receipt", null=True, blank=True
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="submission_receipts",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(channel="manual", owner_id__isnull=True)
                    | models.Q(channel="telegram_bot", owner_id__gt=0)
                    & models.Q(owner_id__isnull=False)
                ),
                name="crm_receipt_owner_matches_channel",
            )
        ]

    def __str__(self):
        return str(self.pk)


class CRMState(models.Model):
    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    last_arrival_sequence = models.PositiveBigIntegerField(default=0)
    demo_seeded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return str(self.pk)


class MutationReceipt(models.Model):
    class Kind(models.TextChoices):
        UPDATE_LEAD = "update_lead", "Изменение заявки"
        CHANGE_STATUS = "change_status", "Смена статуса"
        DELETE_LEAD = "delete_lead", "Удаление заявки"
        CREATE_TAG = "create_tag", "Создание тега"
        DELETE_TAG = "delete_tag", "Удаление тега"

    operation_id = models.UUIDField(primary_key=True)
    kind = models.CharField(max_length=20, choices=Kind)
    target_id = models.CharField(max_length=40)
    request_hash = models.CharField(max_length=64)
    applied_version = models.PositiveBigIntegerField(null=True, blank=True)
    result = models.JSONField(default=dict)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="mutation_receipts",
    )

    class Meta:
        ordering = ["created_at", "operation_id"]

    def __str__(self):
        return str(self.pk)
