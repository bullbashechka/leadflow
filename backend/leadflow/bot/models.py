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


class BotPollingState(models.Model):
    bot_id = models.PositiveBigIntegerField(primary_key=True)
    next_offset = models.BigIntegerField(null=True, blank=True)

    def __str__(self):
        return str(self.pk)


class ProcessedUpdate(models.Model):
    polling_state = models.ForeignKey(BotPollingState, on_delete=models.CASCADE)
    update_id = models.BigIntegerField()
    next_ordinal = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["polling_state", "update_id"],
                name="bot_processed_update_unique",
            ),
        ]
        indexes = [models.Index(fields=["created_at"], name="bot_processed_created_idx")]

    def __str__(self):
        return f"{self.polling_state_id}:{self.update_id}"


class OutboundMessage(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает отправки"
        DELIVERED = "delivered", "Доставлено"
        FAILED = "failed", "Ошибка доставки"

    processed_update = models.ForeignKey(ProcessedUpdate, on_delete=models.CASCADE)
    ordinal = models.PositiveSmallIntegerField()
    chat_id = models.BigIntegerField()
    text = models.TextField()
    reply_markup = models.JSONField(default=dict)
    submission_id = models.UUIDField(null=True, blank=True)
    draft_revision = models.PositiveIntegerField(null=True, blank=True)
    bind_question = models.BooleanField(default=False)
    status = models.CharField(max_length=12, choices=Status, default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField()
    last_error = models.CharField(max_length=60, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["processed_update", "ordinal"],
                name="bot_outbound_message_order",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "next_attempt_at", "id"],
                name="bot_outbound_pending_idx",
            ),
        ]

    def __str__(self):
        return f"{self.processed_update_id}:{self.ordinal}"


class Draft(models.Model):
    class Step(models.TextChoices):
        NAME = "name", "Имя"
        CONTACTS = "contacts", "Контакты"
        CONTACT_CHOICE = "contact_choice", "Добавить или продолжить"
        DIRECTION = "direction", "Направление"
        REQUEST = "request", "Запрос"
        REVIEW = "review", "Проверка"
        PAUSED = "paused", "Пауза"

    class EditMode(models.TextChoices):
        NONE = "", "Нет"
        REPLACE = "replace", "Заменить"
        APPEND = "append", "Дополнить"
        CONTACT_CHOICE = "contact_choice", "Вернуться к контактам"

    class PendingAction(models.TextChoices):
        NONE = "", "Нет"
        CANCEL = "cancel", "Отменить"
        RESTART = "restart", "Начать заново"

    submission_id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(BotUser, on_delete=models.CASCADE, related_name="draft")
    values = models.JSONField(default=dict)
    step = models.CharField(max_length=20, choices=Step, default=Step.NAME)
    revision = models.PositiveIntegerField(default=0)
    editing_field = models.CharField(max_length=20, blank=True)
    edit_mode = models.CharField(max_length=20, choices=EditMode, default=EditMode.NONE)
    contact_index = models.PositiveIntegerField(null=True, blank=True)
    return_step = models.CharField(max_length=20, choices=Step, blank=True)
    pending_action = models.CharField(max_length=10, choices=PendingAction, blank=True)
    question_id = models.BigIntegerField(null=True, blank=True)
    question_date = models.DateTimeField(null=True, blank=True)
    submission_state = models.CharField(max_length=20, default="collecting")
    pending_revision = models.PositiveIntegerField(null=True, blank=True)
    pending_update = models.ForeignKey(
        ProcessedUpdate,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="pending_drafts",
    )
    submission_attempts = models.PositiveSmallIntegerField(default=0)
    submission_retry_at = models.DateTimeField(null=True, blank=True)

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
                        "paused",
                    ]
                ),
                name="bot_draft_known_step",
            ),
            models.CheckConstraint(
                condition=models.Q(submission_state__in=["collecting", "pending"]),
                name="bot_draft_submission_state",
            ),
        ]

    def __str__(self):
        return str(self.pk)
