"""Advertising report: spend per platform/campaign/day, how it relates to the store's own numbers, and
whether the platforms' claims match what the shop actually sees.

Correlations are reported honestly: Pearson r on daily values, the same after removing the weekday pattern
(otherwise shared weekly seasonality looks like cause and effect), the best lag of 0-3 days, and the sample
size. Correlation is evidence of association, not proof that spend caused the sales.
"""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone as dt_timezone
from math import sqrt

from core.models import AdDaily, Order, Store
from core.services import profit

PLATFORM_NAMES = {"google": "Google Ads", "meta": "Meta", "tiktok": "TikTok"}


def _f(x) -> float:
    return float(x or 0)


def pearson(xs: list[float], ys: list[float]):
    n = len(xs)
    if n < 8:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sqrt(sx * sy)


def slope(xs: list[float], ys: list[float]):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    return (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx, my - (sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sx) * mx) if sx else (0.0, my)


def strength(r) -> str:
    if r is None:
        return "not enough data"
    a = abs(r)
    return "none" if a < 0.2 else "weak" if a < 0.4 else "moderate" if a < 0.6 else "strong" if a < 0.8 else "very strong"


def _deseason(values: list[float], dates: list[date]) -> list[float]:
    """Divide each value by its weekday's average so a shared Mon-Sun rhythm cannot fake a relationship."""
    by_wd = defaultdict(list)
    for v, d in zip(values, dates):
        by_wd[d.weekday()].append(v)
    mean = {k: (sum(v) / len(v)) or 1.0 for k, v in by_wd.items()}
    return [v / mean[d.weekday()] for v, d in zip(values, dates)]


def _corr(label: str, x_name: str, y_name: str, x: list[float], y: list[float], dates: list[date]) -> dict:
    """Best of lags 0..3 days (spend leads outcome), raw and weekday-adjusted."""
    best = {"lag": 0, "r": None}
    for lag in range(0, 4):
        xs, ys = (x[: len(x) - lag] if lag else x), y[lag:]
        r = pearson(xs, ys)
        if r is not None and (best["r"] is None or abs(r) > abs(best["r"])):
            best = {"lag": lag, "r": r}
    lag = best["lag"]
    xs, ys, ds = (x[: len(x) - lag] if lag else x), y[lag:], dates[lag:]
    r_adj = pearson(_deseason(xs, dates[: len(xs)]), _deseason(ys, ds)) if len(xs) >= 8 else None
    sl = r2 = None
    if best["r"] is not None:
        sl = slope(xs, ys)[0]
        r2 = best["r"] ** 2
    use = r_adj if r_adj is not None else best["r"]
    return {
        "label": label, "x": x_name, "y": y_name, "n": len(xs), "lag_days": lag,
        "r": None if best["r"] is None else round(best["r"], 3),
        "r_adjusted": None if r_adj is None else round(r_adj, 3),
        "strength": strength(use), "direction": None if use is None else ("positive" if use > 0 else "negative"),
        "slope": None if sl is None else round(sl, 3), "r2": None if r2 is None else round(r2, 3),
    }


def compute(store: Store, days: int = 30) -> dict:
    data = profit.compute(store, days)
    start = date.fromisoformat(data["period"]["start"])
    end = date.fromisoformat(data["period"]["end"])
    series = data["series"]
    dates = [date.fromisoformat(d["date"]) for d in series]
    t = data["totals"]

    rows = list(
        AdDaily.objects.filter(campaign__store=store, date__gte=start, date__lte=end).values(
            "campaign_id", "campaign__platform", "campaign__name", "date", "spend", "impressions", "clicks", "conversions", "conversion_value"
        )
    )
    if not rows:
        return {"has_ads": False, "period": data["period"], "currency": store.currency, "totals": t}

    # ---- per day -----------------------------------------------------------------------------
    spend_d, reported_d = defaultdict(float), defaultdict(float)
    plat_spend_d: dict = defaultdict(lambda: defaultdict(float))
    camp_d: dict = defaultdict(list)
    plat: dict = {}
    for r in rows:
        sp, p = _f(r["spend"]), r["campaign__platform"]
        spend_d[r["date"]] += sp
        reported_d[r["date"]] += _f(r["conversion_value"])
        plat_spend_d[p][r["date"]] += sp
        camp_d[r["campaign_id"]].append((r["date"], sp, _f(r["conversion_value"])))
        a = plat.setdefault(p, {"platform": p, "name": PLATFORM_NAMES.get(p, p), "spend": 0.0, "impressions": 0, "clicks": 0, "conversions": 0.0, "reported_sales": 0.0})
        a["spend"] += sp; a["impressions"] += r["impressions"]; a["clicks"] += r["clicks"]
        a["conversions"] += _f(r["conversions"]); a["reported_sales"] += _f(r["conversion_value"])

    # orders per day per channel actually seen in the store
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=dt_timezone.utc)
    end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=dt_timezone.utc)
    ch_orders_d: dict = defaultdict(lambda: defaultdict(int))
    for o in Order.objects.filter(store=store, cancelled=False, created_at__gte=start_dt, created_at__lt=end_dt).values("created_at", "source"):
        ch_orders_d[o["source"] or "direct"][o["created_at"].astimezone(dt_timezone.utc).date()] += 1
    ch_rev = {c["name"]: c for c in data["channels"]}

    daily = []
    for d, row in zip(dates, series):
        sp = spend_d.get(d, 0.0)
        daily.append({
            "date": d.isoformat(), "spend": round(sp, 2), "reported_sales": round(reported_d.get(d, 0.0), 2),
            "revenue": row["revenue"], "orders": row["orders"], "profit": row["profit"],
            "profit_before_ads": round(row["profit"] + row["ad_spend"], 2),
            "aov": round(row["revenue"] / row["orders"], 2) if row["orders"] else None,
            "mer": round(row["revenue"] / sp, 2) if sp else None,
            **{f"spend_{p}": round(plat_spend_d[p].get(d, 0.0), 2) for p in plat},
        })

    # ---- platforms ----------------------------------------------------------------------------
    margin = (t["contribution"] / t["revenue"]) if t["revenue"] else 0
    platforms = []
    for p, a in plat.items():
        ch = ch_rev.get(p)
        actual_orders = sum(ch_orders_d.get(p, {}).values()) if p in ch_orders_d else None
        gp = a["reported_sales"] * margin
        platforms.append({
            **{k: (round(v, 2) if isinstance(v, float) else v) for k, v in a.items()},
            "share_of_spend_pct": round(100 * a["spend"] / t["ad_spend"], 1) if t["ad_spend"] else 0,
            "ctr_pct": round(100 * a["clicks"] / a["impressions"], 2) if a["impressions"] else None,
            "cpc": round(a["spend"] / a["clicks"], 2) if a["clicks"] else None,
            "cpa": round(a["spend"] / a["conversions"], 2) if a["conversions"] else None,
            "roas": round(a["reported_sales"] / a["spend"], 2) if a["spend"] else None,
            "profit_after_ads": round(gp - a["spend"], 2),
            "store_orders": actual_orders, "store_revenue": ch["revenue"] if ch else None,
            # >1 means the platform claims more sales than the store records for that channel
            "claim_vs_store": round(a["reported_sales"] / ch["revenue"], 2) if ch and ch["revenue"] else None,
            "conversions_vs_orders": round(a["conversions"] / actual_orders, 2) if actual_orders else None,
        })
    platforms.sort(key=lambda x: -x["spend"])

    # ---- campaigns incl. diminishing returns --------------------------------------------------
    campaigns = []
    for c in data["campaigns"]:
        pts = sorted(camp_d.get(c["id"], []))
        sp = [p[1] for p in pts]
        roas_d = [(p[2] / p[1]) if p[1] else 0 for p in pts]
        r = pearson(sp, roas_d)
        half = len(pts) // 2
        s1, v1 = sum(p[1] for p in pts[:half]), sum(p[2] for p in pts[:half])
        s2, v2 = sum(p[1] for p in pts[half:]), sum(p[2] for p in pts[half:])
        campaigns.append({
            **{k: c[k] for k in ("id", "platform", "name", "objective", "spend", "impressions", "clicks", "conversions", "conversion_value",
                                "roas", "break_even_roas", "profit_after_ads", "cpc", "cpa", "ctr_pct")},
            "share_of_spend_pct": round(100 * c["spend"] / t["ad_spend"], 1) if t["ad_spend"] else 0,
            "spend_vs_roas_r": None if r is None else round(r, 2),
            "roas_first_half": round(v1 / s1, 2) if s1 else None, "roas_second_half": round(v2 / s2, 2) if s2 else None,
            "verdict": ("loses money" if c["profit_after_ads"] < 0 else
                        "scale" if c["break_even_roas"] and c["roas"] and c["roas"] >= c["break_even_roas"] * 1.8 else "holding"),
        })

    # ---- correlations ----------------------------------------------------------------------------
    sp_list = [d["spend"] for d in daily]
    corr = [
        _corr("Ad spend vs revenue", "Daily ad spend", "Daily revenue", sp_list, [d["revenue"] for d in daily], dates),
        _corr("Ad spend vs orders", "Daily ad spend", "Daily orders", sp_list, [float(d["orders"]) for d in daily], dates),
        _corr("Ad spend vs profit before ads", "Daily ad spend", "Daily profit before ads", sp_list, [d["profit_before_ads"] for d in daily], dates),
        _corr("Ad spend vs average order value", "Daily ad spend", "Average order value", sp_list, [d["aov"] or 0.0 for d in daily], dates),
    ]
    for p in plat:
        if p in ch_orders_d:
            corr.append(_corr(f"{PLATFORM_NAMES.get(p, p)} spend vs its own orders", f"{PLATFORM_NAMES.get(p, p)} daily spend",
                              f"Orders from {p}", [plat_spend_d[p].get(d, 0.0) for d in dates], [float(ch_orders_d[p].get(d, 0)) for d in dates], dates))

    pts = [{"date": d["date"], "spend": d["spend"], "revenue": d["revenue"]} for d in daily if d["spend"] > 0]
    scatter = {"points": pts}
    if len(pts) >= 8:
        sl, ic = slope([p["spend"] for p in pts], [p["revenue"] for p in pts])
        scatter.update({"slope": round(sl, 3), "intercept": round(ic, 2)})

    # ---- weekly ------------------------------------------------------------------------------------
    weekly = []
    for i in range(0, len(daily), 7):
        ch = daily[i:i + 7]
        sp, rev = sum(x["spend"] for x in ch), sum(x["revenue"] for x in ch)
        weekly.append({"from": ch[0]["date"], "spend": round(sp, 2), "revenue": round(rev, 2), "orders": sum(x["orders"] for x in ch),
                       "profit": round(sum(x["profit"] for x in ch), 2), "mer": round(rev / sp, 2) if sp else None})

    prev = data.get("previous") or {}
    kpis = {
        "spend": t["ad_spend"], "spend_prev": prev.get("ad_spend"),
        "reported_sales": round(sum(p["reported_sales"] for p in platforms), 2), "roas": t["roas"],
        "break_even_roas": round(1 / margin, 2) if margin > 0 else None, "mer": t["mer"], "mer_prev": prev.get("mer"),
        "profit_after_ads_reported": round(sum(p["profit_after_ads"] for p in platforms), 2),
        "store_profit": t["profit"], "spend_share_of_revenue_pct": round(100 * t["ad_spend"] / t["revenue"], 1) if t["revenue"] else None,
        "contribution_margin_pct": t["contribution_margin_pct"],
        "losing_spend": round(sum(c["spend"] for c in campaigns if c["profit_after_ads"] < 0), 2),
        "losing_amount": round(-sum(c["profit_after_ads"] for c in campaigns if c["profit_after_ads"] < 0), 2),
        "clicks": sum(p["clicks"] for p in platforms), "impressions": sum(p["impressions"] for p in platforms),
        "conversions": round(sum(p["conversions"] for p in platforms), 1),
    }
    return {
        "has_ads": True, "period": data["period"], "currency": store.currency, "kpis": kpis, "platforms": platforms,
        "campaigns": campaigns, "daily": daily, "weekly": weekly, "correlations": corr, "scatter": scatter,
        "totals": t,
    }
