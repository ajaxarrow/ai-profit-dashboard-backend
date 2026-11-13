"""Profit engine.

Everything the dashboard, insights and the AI say about money comes from `compute()`.
The AI never calculates; it only explains numbers produced here.

Definitions (all per period, in store currency):
  net revenue     = qty * unit_price - discount          (excl. tax and shipping charged)
  refunds         = refunded_qty * net revenue per unit
  COGS            = (qty - refunded_qty) * unit_cost      (refunded goods assumed restocked)
  payment fees    = order fee, allocated to lines by revenue share
  shipping result = shipping_cost - shipping_charged, allocated by revenue share
  return handling = refunded_qty * store.return_handling_cost
  contribution    = net revenue - refunds - COGS - fees - shipping result - return handling
  profit          = contribution - ad spend (product-linked campaigns land on their product,
                    the rest stays at store level)
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.utils import timezone

from core.models import AdDaily, OrderLine, Store

ZERO = Decimal("0")


def _f(x) -> float:
    return float(x or 0)


def _empty_product(pid, title, sku, category):
    return {
        "id": pid, "title": title, "sku": sku, "category": category,
        "units": 0, "orders": 0, "gross_revenue": 0.0, "discounts": 0.0, "net_revenue": 0.0,
        "refunds": 0.0, "refunded_units": 0, "cogs": 0.0, "fees": 0.0, "shipping": 0.0,
        "return_cost": 0.0, "ad_spend": 0.0, "missing_cost": False,
    }


def _finish_product(p):
    p["revenue"] = round(p["net_revenue"] - p["refunds"], 2)  # revenue kept after refunds
    p["contribution"] = round(
        p["revenue"] - p["cogs"] - p["fees"] - p["shipping"] - p["return_cost"], 2
    )
    p["profit"] = round(p["contribution"] - p["ad_spend"], 2)
    p["margin_pct"] = round(100 * p["profit"] / p["revenue"], 1) if p["revenue"] > 0 else None
    p["refund_rate_pct"] = round(100 * p["refunded_units"] / p["units"], 1) if p["units"] else 0.0
    p["discount_rate_pct"] = round(100 * p["discounts"] / p["gross_revenue"], 1) if p["gross_revenue"] else 0.0
    for k in ("gross_revenue", "discounts", "net_revenue", "refunds", "cogs", "fees", "shipping", "return_cost", "ad_spend"):
        p[k] = round(p[k], 2)
    return p


def resolve_period(days: int):
    """Window = the last `days` days up to and including today (UTC)."""
    today = timezone.now().astimezone(dt_timezone.utc).date()
    start = today - timedelta(days=days - 1)
    return start, today


def compute(store: Store, days: int = 30, include_previous: bool = True) -> dict:
    start, end = resolve_period(days)
    data = _compute_window(store, start, end)
    if include_previous:
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
        prev = _compute_window(store, prev_start, prev_end)
        data["previous"] = prev["totals"]
    data["period"] = {"days": days, "start": start.isoformat(), "end": end.isoformat()}
    return data


def _compute_window(store: Store, start: date, end: date) -> dict:
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=dt_timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=dt_timezone.utc)

    lines = list(
        OrderLine.objects.filter(
            order__store=store, order__cancelled=False,
            order__created_at__gte=start_dt, order__created_at__lt=end_dt,
        ).values(
            "order_id", "product_id", "title", "quantity", "unit_price", "discount", "unit_cost",
            "refunded_quantity", "product__title", "product__sku", "product__category",
            "order__created_at", "order__source", "order__payment_fee",
            "order__shipping_cost", "order__shipping_charged",
        )
    )

    # Order-level net revenue, needed to allocate fees and shipping across lines.
    order_net = defaultdict(float)
    for l in lines:
        order_net[l["order_id"]] += _f(l["quantity"]) * _f(l["unit_price"]) - _f(l["discount"])
    order_line_count = defaultdict(int)
    for l in lines:
        order_line_count[l["order_id"]] += 1

    ret_cost = _f(store.return_handling_cost)
    products: dict = {}
    daily: dict = defaultdict(lambda: {"revenue": 0.0, "contribution": 0.0, "orders": set()})
    channels: dict = defaultdict(lambda: {"revenue": 0.0, "contribution": 0.0, "orders": set()})
    categories: dict = defaultdict(lambda: {"revenue": 0.0, "contribution": 0.0})
    seen_orders_per_product = defaultdict(set)

    for l in lines:
        qty = l["quantity"]
        gross = qty * _f(l["unit_price"])
        disc = _f(l["discount"])
        net = gross - disc
        unit_net = net / qty if qty else 0
        refunded_units = min(l["refunded_quantity"], qty)
        refunds = refunded_units * unit_net
        cost_known = l["unit_cost"] is not None
        cogs = (qty - refunded_units) * _f(l["unit_cost"])

        total_net = order_net[l["order_id"]]
        share = net / total_net if total_net > 0 else 1 / order_line_count[l["order_id"]]
        fees = _f(l["order__payment_fee"]) * share
        shipping = (_f(l["order__shipping_cost"]) - _f(l["order__shipping_charged"])) * share
        return_cost = refunded_units * ret_cost

        pid = l["product_id"] or f"x-{l['title']}"
        p = products.get(pid)
        if p is None:
            p = products[pid] = _empty_product(
                l["product_id"], l["product__title"] or l["title"] or "Unknown product",
                l["product__sku"] or "", l["product__category"] or "Uncategorised",
            )
        p["units"] += qty
        p["gross_revenue"] += gross
        p["discounts"] += disc
        p["net_revenue"] += net
        p["refunds"] += refunds
        p["refunded_units"] += refunded_units
        p["cogs"] += cogs
        p["fees"] += fees
        p["shipping"] += shipping
        p["return_cost"] += return_cost
        if not cost_known:
            p["missing_cost"] = True
        seen_orders_per_product[pid].add(l["order_id"])

        contribution = net - refunds - cogs - fees - shipping - return_cost
        day = l["order__created_at"].astimezone(dt_timezone.utc).date()
        d = daily[day]
        d["revenue"] += net - refunds
        d["contribution"] += contribution
        d["orders"].add(l["order_id"])
        ch = channels[l["order__source"] or "direct"]
        ch["revenue"] += net - refunds
        ch["contribution"] += contribution
        ch["orders"].add(l["order_id"])
        cat = categories[p["category"]]
        cat["revenue"] += net - refunds
        cat["contribution"] += contribution

    for pid, orders in seen_orders_per_product.items():
        products[pid]["orders"] = len(orders)

    # ---- ads ---------------------------------------------------------------
    ad_rows = list(
        AdDaily.objects.filter(campaign__store=store, date__gte=start, date__lte=end).values(
            "campaign_id", "campaign__platform", "campaign__name", "campaign__objective",
            "campaign__product_id", "date", "spend", "impressions", "clicks", "conversions", "conversion_value",
        )
    )
    camp: dict = {}
    ad_daily: dict = defaultdict(float)
    for r in ad_rows:
        c = camp.setdefault(r["campaign_id"], {
            "id": r["campaign_id"], "platform": r["campaign__platform"], "name": r["campaign__name"],
            "objective": r["campaign__objective"], "product_id": r["campaign__product_id"],
            "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0, "conversion_value": 0.0,
        })
        c["spend"] += _f(r["spend"])
        c["impressions"] += r["impressions"]
        c["clicks"] += r["clicks"]
        c["conversions"] += _f(r["conversions"])
        c["conversion_value"] += _f(r["conversion_value"])
        ad_daily[r["date"]] += _f(r["spend"])

    total_spend = sum(c["spend"] for c in camp.values())
    # direct attribution: product-linked campaigns charge their product
    for c in camp.values():
        if c["product_id"] and c["product_id"] in products:
            products[c["product_id"]]["ad_spend"] += c["spend"]
    direct_spend = sum(c["spend"] for c in camp.values() if c["product_id"] and c["product_id"] in products)

    for p in products.values():
        _finish_product(p)

    revenue = sum(p["revenue"] for p in products.values())
    contribution = sum(p["contribution"] for p in products.values())
    profit = contribution - total_spend
    margin_ratio = contribution / revenue if revenue > 0 else 0.0  # contribution margin before ads
    order_count = len({l["order_id"] for l in lines})

    # campaign verdicts: platform "ROAS" is self-reported; what matters is margin earned per franc spent
    prod_by_id = {p["id"]: p for p in products.values() if p["id"]}
    campaigns_out = []
    for c in camp.values():
        pm = prod_by_id.get(c["product_id"])
        ratio = (pm["contribution"] / pm["revenue"]) if pm and pm["revenue"] > 0 else margin_ratio
        gp = c["conversion_value"] * ratio
        c["roas"] = round(c["conversion_value"] / c["spend"], 2) if c["spend"] else None
        c["break_even_roas"] = round(1 / ratio, 2) if ratio > 0 else None
        c["margin_ratio_pct"] = round(100 * ratio, 1)
        c["profit_after_ads"] = round(gp - c["spend"], 2)
        c["cpc"] = round(c["spend"] / c["clicks"], 2) if c["clicks"] else None
        c["ctr_pct"] = round(100 * c["clicks"] / c["impressions"], 2) if c["impressions"] else None
        c["cpa"] = round(c["spend"] / c["conversions"], 2) if c["conversions"] else None
        for k in ("spend", "conversion_value", "conversions"):
            c[k] = round(c[k], 2)
        campaigns_out.append(c)
    campaigns_out.sort(key=lambda c: -c["spend"])

    series = []
    d = start
    while d <= end:
        row = daily.get(d)
        rev = row["revenue"] if row else 0.0
        con = row["contribution"] if row else 0.0
        sp = ad_daily.get(d, 0.0)
        series.append({
            "date": d.isoformat(), "revenue": round(rev, 2), "ad_spend": round(sp, 2),
            "profit": round(con - sp, 2), "orders": len(row["orders"]) if row else 0,
        })
        d += timedelta(days=1)

    def pack(dct):
        out = []
        for k, v in dct.items():
            row = {"name": k, "revenue": round(v["revenue"], 2), "contribution": round(v["contribution"], 2)}
            if "orders" in v:
                row["orders"] = len(v["orders"])
            out.append(row)
        return sorted(out, key=lambda r: -r["revenue"])

    platform_spend = defaultdict(float)
    for c in camp.values():
        platform_spend[c["platform"]] += c["spend"]

    totals = {
        "orders": order_count,
        "units": sum(p["units"] for p in products.values()),
        "gross_revenue": round(sum(p["gross_revenue"] for p in products.values()), 2),
        "discounts": round(sum(p["discounts"] for p in products.values()), 2),
        "net_revenue": round(sum(p["net_revenue"] for p in products.values()), 2),
        "refunds": round(sum(p["refunds"] for p in products.values()), 2),
        "revenue": round(revenue, 2),
        "cogs": round(sum(p["cogs"] for p in products.values()), 2),
        "fees": round(sum(p["fees"] for p in products.values()), 2),
        "shipping": round(sum(p["shipping"] for p in products.values()), 2),
        "return_cost": round(sum(p["return_cost"] for p in products.values()), 2),
        "contribution": round(contribution, 2),
        "ad_spend": round(total_spend, 2),
        "ad_spend_unattributed": round(total_spend - direct_spend, 2),
        "profit": round(profit, 2),
        "margin_pct": round(100 * profit / revenue, 1) if revenue > 0 else None,
        "contribution_margin_pct": round(100 * margin_ratio, 1),
        "aov": round(revenue / order_count, 2) if order_count else None,
        "roas": round(sum(c["conversion_value"] for c in camp.values()) / total_spend, 2) if total_spend else None,
        "mer": round(revenue / total_spend, 2) if total_spend else None,  # revenue / total ad spend
        "refund_rate_pct": round(100 * sum(p["refunded_units"] for p in products.values()) / max(1, sum(p["units"] for p in products.values())), 1),
    }

    return {
        "totals": totals,
        "products": sorted(products.values(), key=lambda p: -p["profit"]),
        "campaigns": campaigns_out,
        "series": series,
        "channels": pack(channels),
        "categories": pack(categories),
        "ad_spend_by_platform": {k: round(v, 2) for k, v in platform_spend.items()},
        "missing_cost_products": [p["title"] for p in products.values() if p["missing_cost"]],
    }
