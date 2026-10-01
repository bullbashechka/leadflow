from django.contrib.auth.forms import AdminUserCreationForm
from django.contrib.auth.forms import UserChangeForm

from .models import User


class UserAdminChangeForm(UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User


class UserAdminCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
