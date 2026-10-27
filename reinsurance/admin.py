from django.contrib import admin

from . import models as m

admin.site.register([m.Reinsurer, m.Treaty, m.Cession, m.MDPInstallment])
