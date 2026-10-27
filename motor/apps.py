from django.apps import AppConfig


class MotorConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "motor"

    def ready(self):
        from django.db.models.signals import post_save

        from .models import Vehicle

        def first_owner(sender, instance, created, **kw):
            if created:
                from .vehicles import open_ownership
                from core import clock
                open_ownership(instance, instance.customer, clock.today(), "First insured")

        post_save.connect(first_owner, sender=Vehicle, dispatch_uid="vehicle_first_owner", weak=False)
