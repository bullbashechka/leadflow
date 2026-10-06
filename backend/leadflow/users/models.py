from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager
from django.db import models
from django.db import router
from django.db import transaction
from django.db.models import CharField
from django.utils.translation import gettext_lazy as _

SECURITY_FIELDS = frozenset({"password", "is_active", "is_staff", "is_superuser"})


class UserQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if SECURITY_FIELDS.intersection(kwargs):
            kwargs["auth_revision"] = models.F("auth_revision") + 1
        return super().update(**kwargs)


class RevisionUserManager(UserManager.from_queryset(UserQuerySet)):
    pass


class User(AbstractUser):
    """Technical users managed through Django Admin; no public registration."""

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)
    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]
    auth_revision = models.PositiveBigIntegerField(default=1, editable=False)
    objects = RevisionUserManager()

    def save(self, *args, force_auth_revision=False, **kwargs):
        if self._state.adding:
            return super().save(*args, **kwargs)
        using = kwargs.get("using") or router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            previous = type(self).objects.using(using).select_for_update().get(pk=self.pk)
            fields = kwargs.get("update_fields")
            checked = SECURITY_FIELDS if fields is None else SECURITY_FIELDS.intersection(fields)
            changed = force_auth_revision or any(
                getattr(previous, field) != getattr(self, field) for field in checked
            )
            self.auth_revision = previous.auth_revision + int(changed)
            if changed and fields is not None:
                kwargs["update_fields"] = set(fields) | {"auth_revision"}
            return super().save(*args, **kwargs)
