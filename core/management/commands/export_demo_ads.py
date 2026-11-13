import csv
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import AdDaily, Store


class Command(BaseCommand):
    help = "Write the demo store's ad spend as a CSV (scaled) to import into another store on Data sources."

    def add_arguments(self, p):
        p.add_argument("--out", default="demo-ads.csv")
        p.add_argument("--days", type=int, default=90)
        p.add_argument("--scale", type=float, default=0.12,
                       help="Multiply spend/clicks/sales so ads match the smaller store (demo has ~30 orders/day)")
        p.add_argument("--fit-store", type=int, help="Size the ads to this store's real revenue (overrides --scale)")
        p.add_argument("--spend-pct", type=float, default=25, help="With --fit-store: total ad spend as %% of that store's revenue")

    def handle(self, *a, **o):
        demo = Store.objects.filter(platform="demo").first()
        if not demo:
            raise CommandError("Run seed_demo first.")
        since = timezone.now().date() - timedelta(days=o["days"] - 1)
        s = o["scale"]
        if o["fit_store"]:
            from django.db.models import Sum
            from core.services import profit
            target = Store.objects.get(id=o["fit_store"])
            revenue = profit.compute(target, o["days"], include_previous=False)["totals"]["revenue"]
            demo_spend = float(AdDaily.objects.filter(campaign__store=demo, date__gte=since).aggregate(t=Sum("spend"))["t"] or 0)
            if not revenue or not demo_spend:
                raise CommandError("The target store has no revenue yet in this period.")
            s = (o["spend_pct"] / 100 * revenue) / demo_spend
            self.stdout.write(f"Fitting to {target.name}: revenue {revenue:,.0f}, target spend {o['spend_pct']}% -> scale {s:.5f}")
        rows = AdDaily.objects.filter(campaign__store=demo, date__gte=since).select_related("campaign", "campaign__product").order_by("date")
        with open(o["out"], "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "platform", "campaign", "spend", "impressions", "clicks", "conversions", "conversion_value", "product_sku"])
            for r in rows:
                w.writerow([r.date, r.campaign.platform, r.campaign.name, round(float(r.spend) * s, 2), int(r.impressions * s),
                            int(r.clicks * s), round(float(r.conversions) * s, 2), round(float(r.conversion_value) * s, 2),
                            r.campaign.product.sku if r.campaign.product else ""])
        self.stdout.write(f"Wrote {rows.count()} rows to {o['out']} (scale {s})")
