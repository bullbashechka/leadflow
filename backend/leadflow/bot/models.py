import uuid

from django.db import models


class BotUser(models.Model):
    telegram_id = models.PositiveBigIntegerField(primary_key=True)
    last_receipt = models.ForeignKey(
        "crm.SubmissionReceipt",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )

    def __str__(self):
        return str(self.pk)


class Draft(models.Model):
    class Step(models.TextChoices):
        NAME = "name", "Имя"
        CONTACTS = "contacts", "Контакты"
        CONTACT_CHOICE = "contact_choice", "Добавить или продолжить"
        DIRECTION = "direction", "Направление"
        REQUEST = "request", "Запрос"
        REVIEW = "review", "Проверка"

    submission_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(BotUser, on_delete=models.CASCADE, related_name="draft")
    values = models.JSONField(default=dict)
    step = models.CharField(max_length=20, choices=Step, default=Step.NAME)
    revision = models.PositiveIntegerField(default=0)
    editing_field = models.CharField(max_length=20, blank=True)
    contact_index = models.PositiveIntegerField(null=True, blank=True)
    question_id = models.BigIntegerField(null=True, blank=True)
    question_date = models.DateTimeField(null=True, blank=True)
    submission_state = models.CharField(max_length=20, default="collecting", editable=False)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    step__in=[
                        "name",
                        "contacts",
                        "contact_choice",
                        "direction",
                        "request",
                        "review",
                    ]
                ),
                name="bot_draft_known_step",
            ),
            models.CheckConstraint(
                condition=models.Q(submission_state="collecting"),
                name="bot_draft_active_state",
            ),
        ]

    def __str__(self):
        return str(self.pk)
