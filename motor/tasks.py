"""Optional Celery wrappers. The same logic is available without Celery via
``python manage.py send_renewal_reminders`` (cron / Windows Task Scheduler)."""
try:
    from celery import shared_task
except ImportError:  # Celery is optional
    def shared_task(fn=None, **kw):
        return fn if fn else (lambda f: f)


@shared_task
def send_renewal_reminders():
    from django.core.management import call_command

    call_command("send_renewal_reminders")
