"""Demo data: a Swiss spare-parts shop with deliberate "stories" for the AI to find.

Planted stories (so the analysis has something real to say):
  * Dishwasher Circulation Pump Pro: best seller by revenue, but 30% discounts, ~22% returns and
    heavy Google Shopping spend make it lose money.
  * Washing Machine Drum Bearing Kit: great margin, low returns, tiny Shopping budget -> scale.
  * Universal Dryer Heating Element: profitable but ~19% returned (wrong fitment).
  * Two products without a cost price (profit overstated).
  * Meta Prospecting and TikTok burn money; Meta Retargeting and Google Brand are efficient.
Dates are relative to today, so every period filter always has data.
"""
import random
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from core.connectors.base import Connector, SyncResult
from core.models import AdCampaign, AdDaily, Order, OrderLine, Product, Store

D = Decimal

# title, sku, category, price, cost, weight, discount_prob, discount_pct, refund_prob
PRODUCTS = [
    ("Dishwasher Circulation Pump Pro", "DW-PUMP-PRO", "Dishwasher", 89.0, 61.0, 14, 0.9, 0.30, 0.22),
    ("Dishwasher Door Seal Universal", "DW-SEAL-U", "Dishwasher", 24.9, 7.5, 9, 0.1, 0.10, 0.04),
    ("Dishwasher Spray Arm Lower", "DW-SPRAY-L", "Dishwasher", 32.0, 11.0, 7, 0.1, 0.10, 0.05),
    ("Dishwasher Inlet Valve", "DW-VALVE", "Dishwasher", 38.5, 15.0, 6, 0.1, 0.10, 0.06),
    ("Dishwasher Salt Container Lid", "DW-LID", "Dishwasher", 9.9, 2.4, 5, 0.0, 0.0, 0.02),
    ("Washing Machine Drum Bearing Kit", "WM-BEAR-KIT", "Washing Machine", 54.0, 14.0, 10, 0.05, 0.05, 0.03),
    ("Washing Machine Door Gasket", "WM-GASKET", "Washing Machine", 39.0, 13.0, 9, 0.1, 0.10, 0.05),
    ("Washing Machine Drain Pump", "WM-DRAIN", "Washing Machine", 46.0, 19.0, 11, 0.15, 0.10, 0.07),
    ("Washing Machine Shock Absorber Set", "WM-SHOCK", "Washing Machine", 36.0, 14.0, 6, 0.1, 0.10, 0.06),
    ("Washing Machine Carbon Brushes Pair", "WM-BRUSH", "Washing Machine", 12.5, 2.9, 8, 0.0, 0.0, 0.02),
    ("Washing Machine Door Lock Module", "WM-LOCK", "Washing Machine", 44.0, 18.0, 5, 0.1, 0.10, 0.09),
    ("Washing Machine Inlet Hose 2m", "WM-HOSE", "Washing Machine", 14.9, 4.2, 7, 0.1, 0.10, 0.03),
    ("Universal Dryer Heating Element 2000W", "DR-HEAT-2K", "Dryer", 42.0, 29.0, 9, 0.3, 0.12, 0.19),
    ("Dryer Drum Belt", "DR-BELT", "Dryer", 16.5, 4.0, 8, 0.05, 0.05, 0.03),
    ("Dryer Condenser Filter Foam", "DR-FOAM", "Dryer", 19.9, 5.5, 6, 0.1, 0.10, 0.02),
    ("Dryer Drum Roller Set", "DR-ROLL", "Dryer", 22.5, 7.0, 5, 0.05, 0.05, 0.04),
    ("Dryer Thermal Fuse", "DR-FUSE", "Dryer", 8.9, 1.6, 6, 0.0, 0.0, 0.02),
    ("Refrigerator Door Gasket (various)", "RF-GASKET", "Refrigerator", 34.0, None, 7, 0.1, 0.10, 0.05),
    ("Refrigerator Thermostat", "RF-THERMO", "Refrigerator", 29.0, 10.5, 5, 0.1, 0.10, 0.08),
    ("Refrigerator Water Filter", "RF-FILTER", "Refrigerator", 27.0, 8.0, 10, 0.2, 0.15, 0.02),
    ("Refrigerator Defrost Heater", "RF-DEFROST", "Refrigerator", 31.0, 11.0, 4, 0.1, 0.10, 0.07),
    ("Fridge Shelf Glass Panel", "RF-GLASS", "Refrigerator", 48.0, 24.0, 3, 0.1, 0.10, 0.06),
    ("Oven Heating Element 2200W", "OV-HEAT", "Oven & Cooking", 41.0, 15.0, 7, 0.1, 0.10, 0.07),
    ("Oven Door Hinge Pair", "OV-HINGE", "Oven & Cooking", 26.0, 8.5, 5, 0.05, 0.05, 0.04),
    ("Oven Thermostat Knob Set", "OV-KNOB", "Oven & Cooking", 17.5, None, 6, 0.0, 0.0, 0.03),
    ("Hob Gas Burner Cap Set", "HB-CAP", "Oven & Cooking", 21.0, 6.0, 5, 0.1, 0.10, 0.03),
    ("Oven Light Bulb 25W 300°C", "OV-BULB", "Oven & Cooking", 6.9, 1.2, 8, 0.0, 0.0, 0.01),
    ("Vacuum Cleaner Filter Set 5-pack", "VC-FILTER5", "Vacuum & Small Appliances", 18.9, 4.5, 10, 0.8, 0.28, 0.02),
    ("Vacuum Cleaner Motor", "VC-MOTOR", "Vacuum & Small Appliances", 79.0, 42.0, 3, 0.2, 0.10, 0.12),
    ("Vacuum Cleaner Roller Brush", "VC-BRUSH", "Vacuum & Small Appliances", 23.0, 7.0, 6, 0.1, 0.10, 0.04),
    ("Coffee Machine Descaler 6x", "CM-DESCALE", "Vacuum & Small Appliances", 14.5, 5.0, 9, 0.2, 0.15, 0.01),
    ("Coffee Machine Brew Group", "CM-BREW", "Vacuum & Small Appliances", 52.0, 20.0, 4, 0.1, 0.10, 0.08),
    ("Mixer Replacement Bowl 4.5L", "MX-BOWL", "Vacuum & Small Appliances", 33.0, 12.0, 3, 0.1, 0.10, 0.05),
]

SOURCES = [("google", 35), ("meta", 20), ("organic", 25), ("direct", 10), ("email", 7), ("tiktok", 3)]

# platform, name, objective, daily spend, reported ROAS (start, end), linked sku, cpc
CAMPAIGNS = [
    ("google", "Google Shopping · Pump Pro", "Shopping", 62, (3.2, 3.0), "DW-PUMP-PRO", 0.42),
    ("google", "Google Shopping · Drum Bearing Kit", "Shopping", 14, (9.5, 9.0), "WM-BEAR-KIT", 0.38),
    ("google", "Google Performance Max · All Parts", "PMax", 120, (3.0, 2.6), None, 0.55),
    ("google", "Google Search · Brand", "Search", 18, (13.0, 12.5), None, 0.21),
    ("google", "Google Search · Generic Parts", "Search", 55, (3.6, 3.4), None, 0.88),
    ("meta", "Meta · Prospecting Broad", "Conversions", 95, (1.7, 0.9), None, 0.64),
    ("meta", "Meta · Retargeting 30d", "Conversions", 24, (7.2, 7.0), None, 0.47),
    ("meta", "Meta · Catalog Advantage+", "Catalog", 48, (3.4, 3.5), None, 0.58),
    ("tiktok", "TikTok · Spark Ads Creators", "Traffic", 30, (0.8, 0.6), None, 0.31),
]


def _weighted(rng, items):
    r = rng.uniform(0, sum(w for _, w in items))
    for v, w in items:
        r -= w
        if r <= 0:
            return v
    return items[-1][0]


@transaction.atomic
def load_demo(store: Store, days: int = 180, seed: int = 42) -> SyncResult:
    rng = random.Random(seed)
    # wipe previous demo content for this store
    store.orders.all().delete()
    store.campaigns.all().delete()
    store.products.all().delete()

    products = []
    for title, sku, cat, price, cost, *_ in PRODUCTS:
        products.append(Product.objects.create(
            store=store, external_id=sku, title=title, sku=sku, category=cat, vendor="OEM compatible",
            price=D(str(price)), unit_cost=D(str(cost)) if cost is not None else None,
        ))
    meta = {p.sku: row for p, row in zip(products, PRODUCTS)}
    weights = [(p, row[5]) for p, row in zip(products, PRODUCTS)]

    today = timezone.now().astimezone(dt_timezone.utc).date()
    orders, lines = [], []
    n = 0
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        growth = 0.85 + 0.3 * (days - back) / days
        weekday = [1.1, 1.05, 1.0, 1.0, 1.05, 0.85, 0.8][day.weekday()]
        count = max(4, int(rng.gauss(30 * growth * weekday, 4)))
        for _ in range(count):
            n += 1
            ts = datetime.combine(day, datetime.min.time(), tzinfo=dt_timezone.utc) + timedelta(seconds=rng.randint(0, 86399))
            picked = []
            for _ in range(_weighted(rng, [(1, 62), (2, 28), (3, 10)])):
                p = _weighted(rng, weights)
                if p not in picked:
                    picked.append(p)
            order_lines, sub = [], D("0")
            for p in picked:
                _, _, _, price, _, _, dprob, dpct, rprob = meta[p.sku]
                qty = 2 if rng.random() < 0.12 else 1
                gross = D(str(price)) * qty
                disc = (gross * D(str(round(dpct * rng.uniform(0.8, 1.2), 3)))).quantize(D("0.01")) if rng.random() < dprob else D("0")
                refunded = sum(1 for _ in range(qty) if rng.random() < rprob)
                order_lines.append((p, qty, disc, refunded))
                sub += gross - disc
            shipping = D("6.90") if sub < 100 else D("0")
            o = Order(
                store=store, external_id=f"D-{10000 + n}", name=f"#{10000 + n}", created_at=ts,
                source=_weighted(rng, SOURCES), shipping_charged=shipping,
                shipping_cost=D(str(round(rng.uniform(6.0, 9.0), 2))), tax=(sub * D("0.081")).quantize(D("0.01")),
                payment_fee=((sub + shipping) * D("0.029") + D("0.30")).quantize(D("0.01")),
            )
            orders.append((o, order_lines))

    Order.objects.bulk_create([o for o, _ in orders], batch_size=500)
    saved = {o.external_id: o for o in Order.objects.filter(store=store)}
    for o, order_lines in orders:
        o = saved[o.external_id]
        for p, qty, disc, refunded in order_lines:
            lines.append(OrderLine(
                order=o, product=p, title=p.title, quantity=qty, unit_price=p.price,
                discount=disc, unit_cost=p.unit_cost, refunded_quantity=refunded,
            ))
    OrderLine.objects.bulk_create(lines, batch_size=1000)

    by_sku = {p.sku: p for p in products}
    ad_rows = 0
    for platform, name, objective, spend, (r0, r1), sku, cpc in CAMPAIGNS:
        c = AdCampaign.objects.create(
            store=store, platform=platform, external_id=name, name=name, objective=objective,
            product=by_sku.get(sku),
        )
        rows = []
        for back in range(days - 1, -1, -1):
            day = today - timedelta(days=back)
            t = (days - 1 - back) / (days - 1)
            # Meta prospecting degrades steadily; others drift gently
            roas = (r0 + (r1 - r0) * (t ** 2 if name.startswith("Meta · Prospecting") else t)) * rng.uniform(0.85, 1.15)
            s = spend * rng.uniform(0.8, 1.2) * (1.12 if day.weekday() < 5 else 0.85)
            clicks = int(s / (cpc * rng.uniform(0.9, 1.1)))
            conv_value = s * roas
            rows.append(AdDaily(
                campaign=c, date=day, spend=D(str(round(s, 2))), impressions=int(clicks / rng.uniform(0.012, 0.02)),
                clicks=clicks, conversions=D(str(round(conv_value / 62, 2))), conversion_value=D(str(round(conv_value, 2))),
            ))
        AdDaily.objects.bulk_create(rows)
        ad_rows += len(rows)

    store.last_synced_at = timezone.now()
    res = SyncResult(products=len(products), orders=len(orders), campaigns=len(CAMPAIGNS), ad_rows=ad_rows)
    store.last_sync_message = res.message()
    store.save()
    return res


class DemoConnector(Connector):
    name = "demo"

    def sync(self, days: int = 180) -> SyncResult:
        return load_demo(self.store, days=max(days, 180))
