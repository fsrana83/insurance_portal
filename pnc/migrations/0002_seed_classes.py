from django.db import migrations


def seed(apps, schema_editor):
    from decimal import Decimal as D
    from pnc.classes import CLASSES
    M = apps.get_model("pnc", "PncClass")
    for code, name, grp, label, rate, minp, dp, dm, comm, ref, avg, fields in CLASSES:
        M.objects.get_or_create(code=code, defaults=dict(name=name, group=grp, si_label=label, rate_pct=D(rate), min_premium=D(minp), deductible_pct=D(dp),
                                                         deductible_min=D(dm), commission_pct=D(str(comm)), referral=ref, average_applies=avg, fields=fields))


class Migration(migrations.Migration):
    dependencies = [("pnc", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
