from django.core.management.base import BaseCommand

from core.demo import load_demo


class Command(BaseCommand):
    help = "Load demo customers, policies, receipts, claims and IFRS 17 postings (all flagged is_demo)."

    def add_arguments(self, p):
        p.add_argument("--policies", type=int, default=1000)
        p.add_argument("--claims", type=int, default=300)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--years", type=int, default=5, help="history window in years (1-10)")

    def handle(self, *a, **o):
        def progress(stage, done, total):
            if total and (done == total or done % 100 == 0):
                self.stdout.write(f"  {stage}: {done}/{total}")
        self.stdout.write(self.style.SUCCESS(str(load_demo(o["policies"], o["claims"], o["seed"], progress, o["years"]))))
