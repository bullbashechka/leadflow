from django.db import migrations


def create_demo_principal(apps, schema_editor):
    user = apps.get_model("users", "User")
    user.objects.using(schema_editor.connection.alias).get_or_create(
        username="__leadflow_demo__",
        defaults={"password": "!", "is_staff": False, "is_superuser": False},
    )


class Migration(migrations.Migration):
    dependencies = [("users", "0001_initial")]
    operations = [migrations.RunPython(create_demo_principal, migrations.RunPython.noop)]
