from datetime import date, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from core.templatetags.money import money_text
from motor.models import Policy, ReminderLog


class Command(BaseCommand):
    help = "Send T-30 / T-7 / T-0 renewal reminders to agents/brokers and customers (idempotent)."

    def handle(self, *a, **kw):
        expired = Policy.objects.filter(status="ACTIVE", end_date__lt=date.today()).update(status="EXPIRED")
        sent = 0
        for offset in settings.RENEWAL_REMINDER_DAYS:
            target = date.today() + timedelta(days=offset)
            for p in Policy.objects.filter(status="ACTIVE", end_date=target).select_related("customer", "producer", "currency"):
                log, created = ReminderLog.objects.get_or_create(policy=p, offset_days=offset)
                if not created:
                    continue
                when = "expires today" if offset == 0 else f"expires in {offset} days"
                urgency = "URGENT: " if offset <= 7 else ""
                subject = f"{urgency}Motor policy {p.policy_no} {when}"
                body = f"Policy {p.policy_no} for {p.customer.name} {when} ({p.end_date:%d %b %Y}). Premium {money_text(p.total_payable, p.currency)}."
                recipients = [e for e in (p.customer.email, p.producer.email) if e]
                if recipients:
                    send_mail(subject, body, None, recipients, fail_silently=True)
                sent += 1
        self.stdout.write(self.style.SUCCESS(f"{sent} reminder(s) sent; {expired} policy(ies) marked expired"))
