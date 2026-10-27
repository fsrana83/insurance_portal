from datetime import date

from django.db import migrations


def backfill(apps, schema_editor):
    Vehicle = apps.get_model("motor", "Vehicle")
    Own = apps.get_model("motor", "VehicleOwnership")
    for v in Vehicle.objects.all().iterator():
        if not Own.objects.filter(vehicle=v).exists():
            Own.objects.create(vehicle=v, customer_id=v.customer_id, from_date=v.base_date or date.today(), source="Existing record")


class Migration(migrations.Migration):
    dependencies = [("motor", "0005_vehicle_ownership")]
    operations = [migrations.RunPython(backfill, migrations.RunPython.noop)]
