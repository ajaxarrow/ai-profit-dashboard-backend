from django.core.management.base import BaseCommand

from core.connectors.seed import load_demo
from core.models import Store


class Command(BaseCommand):
    help = "Create (or rebuild) the demo store with seeded orders and ad spend"

    def handle(self, *args, **opts):
        store = Store.objects.filter(platform="demo").first() or Store.objects.create(
            name="Demo Spare Parts Shop", platform="demo", currency="CHF", domain="demo.local")
        self.stdout.write(load_demo(store).message())
