from django.db import migrations


def seed_tags(apps, schema_editor):
    tag = apps.get_model("crm", "Tag")
    for code, name in [
        ("website", "Сайт"), ("advertising", "Реклама"),
        ("automation", "Автоматизация"), ("other", "Другое"),
    ]:
        tag.objects.using(schema_editor.connection.alias).get_or_create(
            code=code, defaults={"name": name},
        )


class Migration(migrations.Migration):
    dependencies = [("crm", "0001_initial")]
    operations = [migrations.RunPython(seed_tags, migrations.RunPython.noop)]
