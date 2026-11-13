import os

from django.core.management.base import BaseCommand, CommandError

from core.connectors.base import ConnectorError
from core.connectors.shopify import ShopifyConnector, normalize_domain
from core.models import Store


class Command(BaseCommand):
    help = "Connect a Shopify store from SEED_SHOPIFY_DOMAIN / _CLIENT_ID / _CLIENT_SECRET (or _ACCESS_TOKEN) in .env"

    def handle(self, *a, **o):
        env = os.environ.get
        try:
            domain = normalize_domain(env("SEED_SHOPIFY_DOMAIN", ""))
            token, cid, secret = env("SEED_SHOPIFY_ACCESS_TOKEN", ""), env("SEED_SHOPIFY_CLIENT_ID", ""), env("SEED_SHOPIFY_CLIENT_SECRET", "")
            if not token and not (cid and secret):
                raise ConnectorError("Set SEED_SHOPIFY_CLIENT_ID and SEED_SHOPIFY_CLIENT_SECRET (or SEED_SHOPIFY_ACCESS_TOKEN).")
            store = Store.objects.filter(platform="shopify", domain=domain).first() or Store(platform="shopify", domain=domain, name=domain)
            store.access_token, store.client_id, store.client_secret = token, cid, secret
            shop = ShopifyConnector(store).test_connection()
            store.name, store.currency = shop["name"], shop["currencyCode"]
            store.save()
        except ConnectorError as e:
            raise CommandError(str(e))
        self.stdout.write(self.style.SUCCESS(f"Connected: {store.name} ({store.domain}, {store.currency}) as store id {store.id}"))
