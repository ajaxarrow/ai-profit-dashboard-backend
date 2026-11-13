from django.core.management.base import BaseCommand

from core.models import Store
from core.services import monitor


class Command(BaseCommand):
    help = "Run the profit monitor for every store (schedule this, e.g. hourly via cron)."

    def add_arguments(self, p):
        p.add_argument("--days", type=int, default=30)
        p.add_argument("--store", type=int)

    def handle(self, *a, **o):
        qs = Store.objects.filter(id=o["store"]) if o["store"] else Store.objects.all()
        for store in qs:
            run = monitor.run(store, o["days"], trigger="schedule")
            self.stdout.write(f"{store.name}: {run.stats} · health {run.health.get('score')}")
