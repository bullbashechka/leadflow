import uuid

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
    source = models.CharField(max_length=20, choices=Source)
    status = models.CharField(max_length=20, choices=Status, default=Status.NEW)
    created_at = models.DateTimeField(default=timezone.now, editable=False)
    tags = models.ManyToManyField(Tag, related_name="leads", blank=True)

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
    lead = models.OneToOneField(Lead, on_delete=models.PROTECT, related_name="receipt")

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
