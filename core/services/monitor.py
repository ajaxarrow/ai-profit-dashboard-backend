"""The monitor: the loop that makes the app proactive instead of a report you open.

  1. compute  - profit engine over the period and over the last two 7-day windows
  2. detect   - rule findings + trend anomalies (profit drop, campaign decay, refund spike)
  3. track    - upsert Signals so they keep identity across runs (new / worsened / resolved)
  4. diagnose - one AI call turns every signal into a scannable card + writes the morning brief
Signals you approved that stop being detected are counted as wins.
"""
from datetime import timedelta
from decimal import Decimal

from django.utils import timezone

from core.ai import analyst
from core.ai.providers import AIError
from core.models import MonitorRun, Signal, Store
from core.services import insights, profit

GROUP = {
    "loss_product": "problem", "loss_campaign": "problem", "returns": "problem", "discount": "problem", "store_mer": "problem",
    "scale_campaign": "opportunity", "data_quality": "risk", "trend_profit": "risk", "trend_campaign": "risk", "trend_refund": "risk",
}


def _clamp(v, lo=0, hi=100):
    return max(lo, min(hi, v))


def _health(data: dict, candidates: list[dict], n_products: int) -> dict:
    t = data["totals"]
    scores = {"Margin": _clamp((t["margin_pct"] or 0) / 25 * 100), "Returns": _clamp(100 - max(0, t["refund_rate_pct"] - 3) * 10)}
    be = 100 / t["contribution_margin_pct"] if t["contribution_margin_pct"] else None
    if t["ad_spend"] and t["mer"] and be:
        scores["Ad efficiency"] = _clamp(t["mer"] / be * 50)
    leak = sum(c["impact"] for c in candidates if c["kind"] in ("loss_product", "loss_campaign"))
    scores["Money leaks"] = _clamp(100 - (leak / t["revenue"] * 500 if t["revenue"] else 0))
    missing = len(data.get("missing_cost_products") or [])
    scores["Cost data"] = _clamp(100 - 100 * missing / max(1, n_products))
    overall = round(sum(scores.values()) / len(scores))
    return {"score": overall, "parts": [{"label": k, "score": round(v)} for k, v in scores.items()],
            "label": "Healthy" if overall >= 75 else "Needs attention" if overall >= 50 else "At risk"}


def _viz(f: dict, data: dict, prods: dict, camps: dict) -> dict:
    t, k = data["totals"], f["kind"]
    p, c = prods.get(f["entity"]), camps.get(f["entity"])
    if k == "loss_product" and p:
        return {"type": "stack", "revenue": p["revenue"], "profit": p["profit"], "parts": [
            {"label": "Product cost", "value": p["cogs"], "tone": "blue"},
            {"label": "Fees, shipping, returns", "value": round(p["fees"] + p["shipping"] + p["return_cost"], 2), "tone": "lav"},
            {"label": "Advertising", "value": p["ad_spend"], "tone": "tan"}]}
    if k in ("loss_campaign", "scale_campaign") and c:
        return {"type": "roas", "label": "ROAS", "roas": c["roas"], "break_even": c["break_even_roas"], "spend": c["spend"],
                "earned": round(c["profit_after_ads"] + c["spend"], 2)}
    if k == "store_mer":
        be = round(100 / t["contribution_margin_pct"], 2) if t["contribution_margin_pct"] else None
        return {"type": "roas", "label": "Revenue per ad franc", "roas": t["mer"], "break_even": be, "spend": t["ad_spend"], "earned": t["contribution"]}
    if k == "returns" and p:
        return {"type": "ring", "value": p["refund_rate_pct"], "benchmark": t["refund_rate_pct"], "label": "of units refunded", "bench_label": "store average"}
    if k == "discount" and p:
        avg = round(100 * t["discounts"] / t["gross_revenue"], 1) if t["gross_revenue"] else 0
        return {"type": "ring", "value": p["discount_rate_pct"], "benchmark": avg, "label": "average discount", "bench_label": "store average"}
    if k == "data_quality":
        return {"type": "chips", "items": (data.get("missing_cost_products") or [])[:6]}
    return {}


def _trend_candidates(store: Store, days: int, data: dict) -> list[dict]:
    end = profit.resolve_period(days)[1]
    a_start, b_end = end - timedelta(days=6), end - timedelta(days=7)
    b_start = b_end - timedelta(days=6)
    cur, prev = profit._compute_window(store, a_start, end), profit._compute_window(store, b_start, b_end)
    scale, cur_t, prev_t, cc = days / 7, cur["totals"], prev["totals"], store.currency
    out = []
    min_spend = max(15.0, min(150.0, 0.045 * data["totals"]["ad_spend"]))
    if prev_t["profit"] > 0 and cur_t["orders"]:
        drop = prev_t["profit"] - cur_t["profit"]
        if drop / prev_t["profit"] >= 0.20 and drop >= max(10.0, 0.01 * prev_t["revenue"] * 4):
            spark = [d["profit"] for d in (prev["series"] + cur["series"])]
            out.append({"kind": "trend_profit", "severity": "high" if drop / prev_t["profit"] >= 0.4 else "medium", "entity": "store",
                        "title": f"Profit fell {drop / prev_t['profit'] * 100:.0f}% this week",
                        "detail": f"{cc} {cur_t['profit']:,.0f} in the last 7 days against {cc} {prev_t['profit']:,.0f} the week before.",
                        "impact": round(drop * scale, 2), "action": "Check which products and campaigns changed this week (see the other signals).",
                        "viz": {"type": "spark", "series": spark, "split": 7, "before": prev_t["profit"], "after": cur_t["profit"], "label": "Daily profit, last 14 days"}})
    pc = {c["name"]: c for c in prev["campaigns"]}
    for c in cur["campaigns"]:
        o = pc.get(c["name"])
        if not o or not c["roas"] or not o["roas"] or c["spend"] < min_spend or o["spend"] < min_spend:
            continue
        if c["roas"] <= o["roas"] * 0.75:
            ratio = c["margin_ratio_pct"] / 100
            loss = (o["roas"] - c["roas"]) * c["spend"] * ratio * scale
            out.append({"kind": "trend_campaign", "severity": "high" if c["roas"] <= o["roas"] * 0.6 else "medium", "entity": c["name"],
                        "title": f"{c['name']} is getting worse",
                        "detail": f"ROAS fell from {o['roas']} to {c['roas']} week on week while spending {cc} {c['spend']:,.0f}.",
                        "impact": round(loss, 2), "action": "Cap the daily budget and refresh creative or audience before it burns more.",
                        "viz": {"type": "delta", "label": "ROAS, previous 7 days to last 7 days", "before": o["roas"], "after": c["roas"], "unit": "x", "bad": "down"}})
    pp = {p["title"]: p for p in prev["products"]}
    for p in cur["products"]:
        o = pp.get(p["title"])
        if o and p["units"] >= 12 and o["units"] >= 12 and p["refund_rate_pct"] - o["refund_rate_pct"] >= 15:
            leak = (p["refunds"] * 0.5 + p["return_cost"]) * scale
            if leak < 100:
                continue
            out.append({"kind": "trend_refund", "severity": "medium", "entity": p["title"],
                        "title": f"Returns of {p['title']} are rising",
                        "detail": f"{p['refund_rate_pct']}% of units refunded this week, up from {o['refund_rate_pct']}%.",
                        "impact": round(leak, 2), "action": "Check for a batch, supplier or listing change; add fitment details to the page.",
                        "viz": {"type": "delta", "label": "Refund rate, previous 7 days to last 7 days", "before": o["refund_rate_pct"], "after": p["refund_rate_pct"], "unit": "%", "bad": "up"}})
    return out


def detect(store: Store, days: int) -> tuple[dict, list[dict]]:
    data = profit.compute(store, days)
    prods, camps = {p["title"]: p for p in data["products"]}, {c["name"]: c for c in data["campaigns"]}
    cands = []
    for f in insights.find(data, store.currency):
        cands.append({**f, "viz": _viz(f, data, prods, camps)})
    cands += _trend_candidates(store, days, data)
    for c in cands:
        c["group"] = GROUP.get(c["kind"], "problem")
        c["fingerprint"] = f"{c['kind']}:{c['entity']}"
    return data, cands


def run(store: Store, days: int = 30, trigger: str = "manual") -> MonitorRun:
    data, cands = detect(store, days)
    now = timezone.now()
    health = _health(data, cands, len(data["products"]))
    run_obj = MonitorRun.objects.create(store=store, period_days=days, trigger=trigger, health=health)

    existing = {s.fingerprint: s for s in Signal.objects.filter(store=store)}
    seen, new, worsened = set(), 0, 0
    for c in cands:
        seen.add(c["fingerprint"])
        s, impact = existing.get(c["fingerprint"]), Decimal(str(c["impact"]))
        if s is None:
            s = Signal(store=store, fingerprint=c["fingerprint"], first_run=run_obj)
            new += 1
        else:
            if s.status == "resolved":  # came back
                s.status, s.approved_at, s.resolved_at, s.approved_impact, s.first_run = "new", None, None, None, run_obj
                new += 1
            if s.impact and impact >= s.impact * Decimal("1.2"):
                worsened += 1
            s.impact_prev = s.impact
        s.kind, s.kind_group, s.severity, s.title, s.entity = c["kind"], c["group"], c["severity"], c["title"], c["entity"]
        s.detail, s.impact, s.viz, s.active = c["detail"], impact, c["viz"], True
        s.ai = s.ai or {"steps": [c["action"]]}
        s._action = c["action"]
        s.save()
        c["obj"] = s

    resolved = wins = 0
    for fp, s in existing.items():
        if fp not in seen and s.active:
            s.active = False
            if s.status != "resolved":
                s.status, s.resolved_at = "resolved", now
                resolved += 1
                wins += 1 if s.approved_at else 0
            s.save()

    # diagnose with AI (one call); fall back to the deterministic text so cards are never empty
    rank = {"high": 0, "medium": 1, "low": 2}
    active = sorted((c["obj"] for c in cands if c["obj"].status != "dismissed"), key=lambda s: (rank[s.severity], -float(s.impact)))[:10]
    brief, provider = {}, ""
    try:
        brief, per, provider = analyst.signals_ai(store, data, active, health)
        for s in active:
            if s.fingerprint in per:
                s.ai = per[s.fingerprint]
                s.save(update_fields=["ai"])
    except AIError:
        pass
    if not brief:
        top = max(cands, key=lambda c: c["impact"], default=None)
        brief = {"headline": f"Profit health is {health['score']}/100 ({health['label'].lower()}).",
                 "bullets": [f"Biggest issue: {top['title']}." if top else "No problems detected."]}
    at_stake = sum(float(s.impact) for s in Signal.objects.filter(store=store, active=True, kind_group__in=["problem", "risk"]).exclude(status="dismissed"))
    run_obj.brief, run_obj.provider = brief, provider
    run_obj.stats = {"new": new, "worsened": worsened, "resolved": resolved, "wins": wins, "at_stake": round(at_stake, 2),
                     "detected": len(cands)}
    run_obj.save()
    return run_obj


def serialize_signal(s: Signal) -> dict:
    return {"id": s.id, "kind": s.kind, "group": s.kind_group, "severity": s.severity, "title": s.title, "entity": s.entity,
            "detail": s.detail, "impact": float(s.impact), "viz": s.viz, "ai": s.ai, "status": s.status, "active": s.active,
            "new": bool(s.first_run_id and s.first_run_id == getattr(s, "_latest_run_id", None)),
            "first_seen": s.first_seen, "approved_at": s.approved_at, "resolved_at": s.resolved_at,
            "approved_impact": float(s.approved_impact) if s.approved_impact is not None else None}


def snapshot(store: Store) -> dict | None:
    """What the Command Center shows."""
    last = MonitorRun.objects.filter(store=store).first()
    if not last:
        return None
    sigs = list(Signal.objects.filter(store=store).exclude(status="dismissed"))
    for s in sigs:
        s._latest_run_id = last.id
    open_ = [s for s in sigs if s.active and s.status != "resolved"]
    wins = [s for s in sigs if s.status == "resolved" and s.approved_at]
    order = {"high": 0, "medium": 1, "low": 2}
    open_.sort(key=lambda s: (s.status == "snoozed", order[s.severity], -float(s.impact)))
    approved = [s for s in open_ if s.status == "approved"]
    return {
        "run": {"id": last.id, "created_at": last.created_at, "period_days": last.period_days, "trigger": last.trigger,
                "health": last.health, "brief": last.brief, "stats": last.stats, "provider": last.provider},
        "signals": [serialize_signal(s) for s in open_],
        "wins": [serialize_signal(s) for s in wins[:5]],
        "loop": {
            "detected": len(open_), "recommended": sum(1 for s in open_ if s.ai.get("headline")),
            "approved": len(approved), "approved_impact": round(sum(float(s.approved_impact or s.impact) for s in approved), 2),
            "fixed": len(wins), "fixed_impact": round(sum(float(s.approved_impact or 0) for s in wins), 2),
        },
    }
