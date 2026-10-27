from django.db import migrations


def fix(apps, schema_editor):
    apps.get_model("motor", "Cover").objects.filter(code="PAB").update(only_products="TPL")


class Migration(migrations.Migration):
    dependencies = [("motor", "0012_cover_only_products")]
    operations = [migrations.RunPython(fix, migrations.RunPython.noop)]
