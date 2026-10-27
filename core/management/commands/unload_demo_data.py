from django.core.management.base import BaseCommand

from core.demo import unload_demo


class Command(BaseCommand):
    help = "Remove all demo records (flag is_demo). Reference data and real records are untouched."

    def add_arguments(self, p):
        p.add_argument("--remove-users", action="store_true", help="Also delete the demo login accounts")

    def handle(self, *a, **o):
        self.stdout.write(self.style.SUCCESS(str(unload_demo(o["remove_users"]))))
