from django.contrib import admin

from . import models

for m in (models.PncClass, models.PncPolicy, models.PncRisk, models.PncClaim, models.PncCession, models.PncEndorsement):
    admin.site.register(m)
