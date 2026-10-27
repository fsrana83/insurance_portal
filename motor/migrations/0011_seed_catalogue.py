from django.db import migrations


def seed(apps, schema_editor):
    from motor.catalogue import DEFAULT
    Make, Model = apps.get_model("motor", "VehicleMake"), apps.get_model("motor", "VehicleModel")
    for mk, mds in DEFAULT.items():
        make, _ = Make.objects.get_or_create(name=mk)
        for md in mds:
            Model.objects.get_or_create(make=make, name=md)


class Migration(migrations.Migration):
    dependencies = [("motor", "0010_release10_phase1")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
