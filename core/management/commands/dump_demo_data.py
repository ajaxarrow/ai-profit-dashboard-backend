import gzip
import json

from django.core import serializers
from django.core.management.base import BaseCommand

from core import models as m

ORDER = [m.Store, m.Product, m.Order, m.OrderLine, m.AdCampaign, m.AdDaily, m.MonitorRun, m.Signal, m.AnalysisReport]
SECRETS = ("access_token", "client_id", "client_secret")


class Command(BaseCommand):
    help = ("Write every store's data to a gzipped fixture for deployment. Shopify credentials are removed and "
            "store names trimmed. Load it with `manage.py loaddata demo`.")

    def add_arguments(self, p):
        p.add_argument("--out", default="core/fixtures/demo.json.gz")

    def handle(self, *a, **o):
        rows = []
        for model in ORDER:
            rows += json.loads(serializers.serialize("json", model.objects.order_by("pk")))
        for r in rows:
            if r["model"] == "core.store":
                for k in SECRETS:
                    r["fields"][k] = ""
                r["fields"]["name"] = r["fields"]["name"].strip()
        with gzip.open(o["out"], "wt", encoding="utf-8", compresslevel=9) as f:
            json.dump(rows, f, separators=(",", ":"))
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(rows)} records to {o['out']} (credentials removed)"))
