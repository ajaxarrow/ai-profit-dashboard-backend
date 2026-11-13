from django.core.management.base import BaseCommand, CommandError

from core.connectors.base import ConnectorError
from core.connectors.shopify import ShopifyConnector
from core.connectors.shopify_seeder import ShopifySeeder, build_orders
from core.models import Store


class Command(BaseCommand):
    help = ("Create demo products and a backdated order history (with refunds and discounts) in a connected "
            "Shopify store. Needs write_products, write_orders, write_inventory on the app.")

    def add_arguments(self, p):
        p.add_argument("--store", type=int, help="Store id (default: first Shopify store)")
        p.add_argument("--orders", type=int, default=300)
        p.add_argument("--days", type=int, default=90)
        p.add_argument("--seed", type=int, default=42)
        p.add_argument("--pace", type=float, default=0, help="Seconds to wait between orders")
        p.add_argument("--force", action="store_true", help="Add orders even if seed orders already exist")
        p.add_argument("--dry-run", action="store_true", help="Show what would be created; call nothing")
        p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    def handle(self, *a, **o):
        qs = Store.objects.filter(platform="shopify")
        store = qs.filter(id=o["store"]).first() if o["store"] else qs.first()
        if not store:
            raise CommandError("No Shopify store connected. Connect it on the Data sources page first.")
        specs = build_orders(o["orders"], o["days"], o["seed"])
        refunds = sum(1 for s in specs if any(l["refunded"] for l in s["lines"]))
        disc = sum(1 for s in specs if s["discount_pct"])
        self.stdout.write(f"Target: {store.name} ({store.domain})")
        self.stdout.write(f"Plan: 33 products with cost per item, {len(specs)} orders over {o['days']} days "
                          f"({specs[0]['processed_at'][:10]} to {specs[-1]['processed_at'][:10]}), {refunds} with refunds, {disc} discounted")
        if o["dry_run"]:
            return
        if not o["yes"] and input("Create these in the live store? Type 'yes': ").strip().lower() != "yes":
            raise CommandError("Aborted.")
        try:
            res = ShopifySeeder(ShopifyConnector(store), log=self.stdout.write).run(
                o["orders"], o["days"], o["seed"], o["pace"], o["force"])
        except ConnectorError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"Done: {res}. Now sync the store from the dashboard (Data sources → Sync now)."))
