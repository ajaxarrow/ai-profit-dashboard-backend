"""The AI analyst. It is grounded: it receives a snapshot of numbers computed by the profit engine
and deterministic findings, and may only reason over those. It never queries or calculates itself."""
import hashlib
import json

from core.ai.providers import AIError, complete
from core.models import AnalysisReport, Store
from core.services import insights, profit


def data_hash(data: dict) -> str:
    """Fingerprint of the numbers: when it changes, saved AI text is stale."""
    raw = json.dumps([data["totals"], data["period"]["days"], len(data["campaigns"])], sort_keys=True, default=str)
    return hashlib.sha1(raw.encode()).hexdigest()


def _weekly(series: list[dict]) -> list[dict]:
    out = []
    for i in range(0, len(series), 7):
        chunk = series[i:i + 7]
        out.append({
            "from": chunk[0]["date"], "revenue": round(sum(d["revenue"] for d in chunk)),
            "ad_spend": round(sum(d["ad_spend"] for d in chunk)), "profit": round(sum(d["profit"] for d in chunk)),
        })
    return out


def build_snapshot(store: Store, days: int, data: dict | None = None) -> dict:
    data = data or profit.compute(store, days)
    findings = insights.find(data, store.currency)
    prods = data["products"]
    series = data["series"]
    best = max(series, key=lambda d: d["profit"])
    worst = min(series, key=lambda d: d["profit"])
    return {
        "currency": store.currency,
        "store": store.name,
        "period": data["period"],
        "totals": {k: v for k, v in data["totals"].items() if k not in ("units", "gross_revenue", "net_revenue", "ad_spend_unattributed")},
        "previous_period": {k: (data.get("previous") or {}).get(k) for k in ("revenue", "profit", "orders", "ad_spend", "margin_pct")},
        "weekly_trend": _weekly(series),
        "best_day": {"date": best["date"], "profit": best["profit"]},
        "worst_day": {"date": worst["date"], "profit": worst["profit"]},
        "product_columns": ["title", "category", "units", "revenue", "profit", "margin_pct", "refund_rate_pct",
                            "discount_rate_pct", "ad_spend", "missing_cost"],
        "products_by_profit": [
            [p["title"], p["category"], p["units"], round(p["revenue"]), round(p["profit"]), p["margin_pct"],
             p["refund_rate_pct"], p["discount_rate_pct"], round(p["ad_spend"]), p["missing_cost"]]
            for p in prods[:30]
        ],
        "campaigns": [
            {k: c[k] for k in ("platform", "name", "spend", "roas", "break_even_roas", "profit_after_ads", "cpc", "cpa")}
            for c in data["campaigns"]
        ],
        "channels": [{"name": c["name"], "revenue": c["revenue"], "contribution": c["contribution"]} for c in data["channels"]],
        "categories": data["categories"],
        "ad_spend_by_platform": data["ad_spend_by_platform"],
        "findings": [{k: f[k] for k in ("id", "severity", "title", "detail", "impact", "action")} for f in findings[:10]],
        "notes": "profit = revenue after refunds - COGS - fees - net shipping - return handling - ad spend. "
                 "Channel/category contribution is before ads. Findings can overlap (a product and its campaign): never add them. "
                 "ROAS is platform-reported; break_even_roas is what covers margin. missing_cost=true means profit is overstated.",
    }


def _dump(snap: dict) -> str:
    return json.dumps(snap, default=str, separators=(",", ":"))


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])


# ---- chat ---------------------------------------------------------------------------------------
CHAT_SYSTEM = """You are the profit analyst inside an e-commerce profit dashboard, talking to the shop owner.
Answer ONLY from the JSON snapshot below. Never invent numbers, products or campaigns. If the snapshot cannot
answer, say exactly what is missing. Quote exact figures with the currency.

Never mention internal ids such as f1 or f2; refer to findings by what they say. Style: lead with the answer in one sentence. Then the evidence as a short list or a small markdown table (max 8
rows). End with one concrete next action when it is relevant. No filler, no restating the question, no emojis.
Sort any ranking by the metric asked, best first. If a figure is overstated (missing cost price) or an ad platform has no data, say so briefly.

Reply with JSON only: {{"answer": "<markdown>", "follow_ups": ["<3 short natural follow-up questions the owner
would ask next, each under 60 characters>"]}}

SNAPSHOT:
{snapshot}"""


def chat(store: Store, messages: list[dict], days: int) -> tuple[dict, str]:
    snap = build_snapshot(store, days)
    history = [{"role": m["role"], "content": str(m["content"])[:4000]}
               for m in messages[-10:] if m.get("role") in ("user", "assistant")]
    if not history or history[-1]["role"] != "user":
        raise AIError("Last message must come from the user")
    text, provider = complete(
        [{"role": "system", "content": CHAT_SYSTEM.format(snapshot=_dump(snap))}] + history, json_mode=True)
    try:
        out = _parse_json(text)
        answer = str(out.get("answer") or "").strip()
        follow = [str(f)[:80] for f in (out.get("follow_ups") or [])][:3]
        if not answer:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        answer, follow = text.strip(), []
    return {"answer": answer, "follow_ups": follow}, provider


# ---- full review ----------------------------------------------------------------------------------
ANALYSIS_SYSTEM = """You are a senior e-commerce profit analyst. From the SNAPSHOT, write the profit review.
Use only the snapshot's numbers. Reply with JSON only, exactly this shape:
{{
  "headline": "one sentence on the store's true profit situation",
  "summary": "2-3 sentences: profit and margin, trend vs previous period, the single biggest issue",
  "losing_money": [{{"title": "...", "detail": "...", "amount": number, "finding_ids": ["f1"]}}],
  "working_well": [{{"title": "...", "detail": "..."}}],
  "recommendations": [{{"priority": 1, "title": "imperative, specific", "why": "evidence with numbers",
                       "action": "exact change to make", "estimated_impact": number, "finding_ids": ["f1"]}}]
}}
Rules: 3-6 recommendations ordered by estimated_impact (store currency, over the period, conservative);
finding_ids must refer to ids in snapshot.findings; amounts are positive numbers; no markdown; if data is
missing (e.g. no ad data), say so in the summary instead of guessing.

SNAPSHOT:
{snapshot}"""


def analyse(store: Store, days: int) -> AnalysisReport:
    data = profit.compute(store, days)
    snap = build_snapshot(store, days, data)
    text, provider = complete(
        [{"role": "system", "content": ANALYSIS_SYSTEM.format(snapshot=_dump(snap))},
         {"role": "user", "content": "Write the profit review."}],
        json_mode=True,
    )
    try:
        content = _parse_json(text)
    except (ValueError, json.JSONDecodeError):
        raise AIError("The model did not return valid JSON. Try again.")
    content["findings"] = snap["findings"]  # evidence travels with the narrative
    content["generated_for"] = snap["period"]
    return AnalysisReport.objects.create(store=store, kind="review", data_hash=data_hash(data),
                                         period_days=days, provider=provider, content=content)


# ---- per-chart notes --------------------------------------------------------------------------------
NOTE_KEYS = ["summary", "breakdown", "daily", "channels", "categories", "top_products", "worst_products",
             "campaigns", "products", "advertising"]

NOTES_SYSTEM = """You write the short "AI read" shown under each chart of a profit dashboard. From the SNAPSHOT,
reply with JSON only, with exactly these keys, each a string of 1-2 sentences (max 45 words):
summary: the overall profit picture and what changed vs the previous period.
breakdown: what eats the most of every sale, and the one cost worth attacking.
daily: the trend in daily profit, best and worst day, anything unusual.
channels: which sales channel earns the most and which looks weak.
categories: which product category earns the most or least.
top_products: why the leading products win.
worst_products: what is wrong with the weakest products and the fix.
campaigns: which campaigns to cut or scale, with numbers.
products: the pattern across the whole product list (returns, discounts, missing costs).
advertising: how ad spend compares with the margin it earns.
Use exact figures from the snapshot with the currency. Say what the chart shows AND what to do. No markdown, no
hedging, never invent data. If a topic has no data (e.g. no ad campaigns), say so in one short sentence.

SNAPSHOT:
{snapshot}"""


def fallback_notes(data: dict, currency: str) -> dict:
    """Deterministic notes used when no AI provider answers, so the UI is never empty."""
    t, c = data["totals"], currency
    m = lambda v: f"{c} {abs(v):,.0f}"
    prev = data.get("previous") or {}
    chg = ""
    if prev.get("profit"):
        d = (t["profit"] - prev["profit"]) / abs(prev["profit"]) * 100
        chg = f", {'up' if d > 0 else 'down'} {abs(d):.0f}% on the previous period"
    s = data["series"]
    best, worst = max(s, key=lambda d: d["profit"]), min(s, key=lambda d: d["profit"])
    costs = {"product cost": t["cogs"], "advertising": t["ad_spend"], "refunds": t["refunds"], "discounts": t["discounts"],
             "fees, shipping and returns": t["fees"] + t["shipping"] + t["return_cost"]}
    big = max(costs, key=costs.get)
    ch, cat = data["channels"], data["categories"]
    prods = data["products"]
    losing = [x for x in data["campaigns"] if x["profit_after_ads"] < 0]
    return {
        "summary": f"Net profit is {m(t['profit'])} ({t['margin_pct']}% of revenue){chg}.",
        "breakdown": f"{big.capitalize()} is the largest cost at {m(costs[big])} of {m(t['gross_revenue'])} gross sales.",
        "daily": f"Best day {best['date']} ({m(best['profit'])}), weakest {worst['date']} ({'-' if worst['profit'] < 0 else ''}{m(worst['profit'])}).",
        "channels": (f"{ch[0]['name'].title()} brings the most revenue ({m(ch[0]['revenue'])})." if ch else "No channel data."),
        "categories": (f"{cat[0]['name']} leads with {m(cat[0]['revenue'])} revenue; {cat[-1]['name']} is smallest." if cat else "No category data."),
        "top_products": (f"{prods[0]['title']} earns the most: {m(prods[0]['profit'])} profit." if prods else "No sales in this period."),
        "worst_products": (f"{prods[-1]['title']} is weakest at {'-' if prods[-1]['profit'] < 0 else ''}{m(prods[-1]['profit'])} profit." if prods else "No sales in this period."),
        "campaigns": (f"{len(losing)} campaign(s) lose {m(sum(x['profit_after_ads'] for x in losing))} after ad cost." if losing else "No campaign is losing money." if data["campaigns"] else "No ad data connected."),
        "products": f"{t['refund_rate_pct']}% of units are refunded across {len(prods)} products.",
        "advertising": (f"Ads cost {m(t['ad_spend'])}; every ad franc returns {t['mer']} in revenue." if t["ad_spend"] else "No ad spend recorded."),
    }


def chart_notes(store: Store, days: int, force: bool = False) -> dict:
    data = profit.compute(store, days)
    h = data_hash(data)
    if not force:
        cached = AnalysisReport.objects.filter(store=store, kind="charts", period_days=days, data_hash=h).first()
        if cached:
            return {"notes": cached.content, "provider": cached.provider, "ai": True, "created_at": cached.created_at}
    if data["totals"]["orders"] == 0:
        return {"notes": fallback_notes(data, store.currency), "provider": None, "ai": False, "created_at": None}
    snap = build_snapshot(store, days, data)
    try:
        text, provider = complete(
            [{"role": "system", "content": NOTES_SYSTEM.format(snapshot=_dump(snap))},
             {"role": "user", "content": "Write the notes."}], json_mode=True)
        notes = _parse_json(text)
        base = fallback_notes(data, store.currency)
        notes = {k: str(notes.get(k) or base[k]) for k in NOTE_KEYS}
    except (AIError, ValueError, json.JSONDecodeError):
        return {"notes": fallback_notes(data, store.currency), "provider": None, "ai": False, "created_at": None}
    rep = AnalysisReport.objects.create(store=store, kind="charts", data_hash=h, period_days=days, provider=provider, content=notes)
    return {"notes": notes, "provider": provider, "ai": True, "created_at": rep.created_at}


# ---- advertising: report notes and scoped chat -------------------------------------------------------
ADS_NOTE_KEYS = ["summary", "platforms", "spend_vs_results", "lag", "diminishing", "attribution", "waste", "campaigns"]

ADS_NOTES_SYSTEM = """You write the short "AI read" under each section of an advertising report for a shop owner.
From the SNAPSHOT reply with JSON only, exactly these keys, each 1-3 sentences (max 55 words):
summary: is advertising paying for itself overall (spend, MER, break-even ROAS, profit), and the single biggest lever.
platforms: which platform earns its spend and which does not, with numbers.
spend_vs_results: what the correlation between daily ad spend and revenue/orders/profit really says. Compare r raw and
  r_adjusted: if raw is clearly higher than adjusted, say the apparent link is mostly a shared weekday pattern. Say plainly
  when the evidence is weak (|r_adjusted| < 0.2) and what that means: extra spend cannot be shown to add sales.
lag: whether sales respond the same day or after a delay (lag_days), or that no delay is detectable.
diminishing: use campaigns' spend_vs_roas_r and roas_first_half/second_half: which campaigns get worse as spend or time goes on.
attribution: compare platform-claimed sales and conversions with what the store recorded (claim_vs_store,
  conversions_vs_orders): ratios above 1 mean over-claiming. Warn which numbers to trust.
waste: how much is spent on campaigns that lose money and what to do with that budget.
campaigns: which to cut, hold and scale, with figures.
Use exact figures with the currency. Write in plain words for a shop owner: never quote field names or variable names (say \"claimed sales versus store sales\", not claim_vs_store; \"r adjusted\" is fine). Correlation is association, never proof of cause: say "suggests" or "no evidence".
No markdown, no filler, never invent data. If a topic lacks data, say so in one short sentence.

SNAPSHOT:
{snapshot}"""

ADS_CHAT_SYSTEM = """You are the advertising analyst inside a profit dashboard, talking to the shop owner.
Answer ONLY from the JSON snapshot below: it holds spend, platform/campaign performance, daily and weekly spend with store
revenue/orders/profit, correlations (r raw, r_adjusted for weekday pattern, best lag, n), and platform claims versus what
the store recorded. Never invent numbers. Quote exact figures with the currency. Write in plain words for a shop owner: never quote field names or variable names (say \"claimed sales versus store sales\", not claim_vs_store; \"r adjusted\" is fine). Correlation is association, not proof
of cause; when |r_adjusted| < 0.2 say there is no reliable evidence that spend drives sales. If asked something the
snapshot cannot answer (e.g. keywords, audiences, creatives), say what data is missing.

Never mention internal ids; refer to things by name. Style: lead with the answer in one sentence, then evidence as a short list or a small markdown table (sorted best first),
then one concrete next action when relevant. No filler, no emojis.

Reply with JSON only: {{"answer": "<markdown>", "follow_ups": ["<3 short follow-up questions, each under 60 characters>"]}}

SNAPSHOT:
{snapshot}"""


def ads_snapshot(report: dict) -> dict:
    campaign_keys = ("platform", "name", "spend", "roas", "break_even_roas", "profit_after_ads", "cpc", "cpa", "share_of_spend_pct",
                     "spend_vs_roas_r", "roas_first_half", "roas_second_half", "verdict")
    plat_keys = ("name", "spend", "share_of_spend_pct", "roas", "profit_after_ads", "cpc", "cpa", "conversions", "reported_sales",
                 "store_orders", "store_revenue", "claim_vs_store", "conversions_vs_orders")
    corr_keys = ("label", "n", "lag_days", "r", "r_adjusted", "strength", "slope")
    return {
        "currency": report["currency"], "period": report["period"], "kpis": report["kpis"],
        "platforms": [{k: p[k] for k in plat_keys} for p in report["platforms"]],
        "campaigns": [{k: c[k] for k in campaign_keys} for c in report["campaigns"]],
        "correlations": [{k: c[k] for k in corr_keys} for c in report["correlations"]],
        "regression_revenue_on_spend": {k: report["scatter"].get(k) for k in ("slope", "intercept")},
        "weekly": report["weekly"],
        "meaning": "slope = extra revenue associated with 1 extra currency unit of daily spend (association only). "
                   "r_adjusted removes the weekday pattern. claim_vs_store > 1 means the platform claims more sales than the store recorded.",
    }


def ads_fallback_notes(report: dict) -> dict:
    """Plain computed sentences for the ads report when no model answers."""
    c, k = report["currency"], report["kpis"]
    m = lambda v: f"{c} {abs(v):,.0f}"
    plats, camps, corr = report["platforms"], report["campaigns"], report["correlations"]
    losing = [x for x in camps if x["profit_after_ads"] < 0]
    scale = [x for x in camps if x["verdict"] == "scale"]
    main = corr[0] if corr else None
    over = [p for p in plats if p.get("claim_vs_store") and p["claim_vs_store"] > 1.25]
    return {
        "summary": f"{m(k['spend'])} of ad spend returned {k['roas']}x reported ROAS against a break-even of {k['break_even_roas']}x; {m(k['losing_amount'])} is lost on losing campaigns.",
        "platforms": "; ".join(f"{p['name']} {m(p['spend'])} at {p['roas']}x ROAS ({'+' if p['profit_after_ads'] >= 0 else '-'}{m(p['profit_after_ads'])} after ads)" for p in plats) + ".",
        "spend_vs_results": (f"Daily ad spend vs revenue: r = {main['r']} raw, {main['r_adjusted']} after removing the weekday pattern ({main['strength']} evidence). Association only." if main and main["r"] is not None else "Not enough days to measure."),
        "lag": (f"Strongest link when spend leads revenue by {main['lag_days']} day(s)." if main and main["lag_days"] else "Sales respond on the same day; no delay is detectable."),
        "diminishing": "; ".join(f"{x['name']} ROAS {x['roas_first_half']}→{x['roas_second_half']}" for x in camps if x["roas_first_half"] and x["roas_second_half"] and x["roas_second_half"] < x["roas_first_half"] * 0.85) or "No campaign shows a clear decline in ROAS over the period.",
        "attribution": ("Platforms over-claim vs the store: " + ", ".join(f"{p['name']} {p['claim_vs_store']}x" for p in over) + "." if over else "Platform-claimed sales are in line with what the store recorded."),
        "waste": f"{m(k['losing_spend'])} is spent on campaigns that lose {m(k['losing_amount'])} after margin.",
        "campaigns": (f"Cut: {', '.join(x['name'] for x in losing) or 'none'}. Scale: {', '.join(x['name'] for x in scale) or 'none'}."),
    }


def ads_notes(store: Store, days: int, report: dict, force: bool = False) -> dict:
    from core.services.ads_report import compute  # noqa: F401  (kept for symmetry; report is passed in)
    h = hashlib.sha1(json.dumps([report["kpis"], days], sort_keys=True, default=str).encode()).hexdigest()
    if not force:
        cached = AnalysisReport.objects.filter(store=store, kind="ads", period_days=days, data_hash=h).first()
        if cached:
            return {"notes": cached.content, "provider": cached.provider, "ai": True}
    try:
        text, provider = complete(
            [{"role": "system", "content": ADS_NOTES_SYSTEM.format(snapshot=_dump(ads_snapshot(report)))},
             {"role": "user", "content": "Write the notes."}], json_mode=True)
        raw = _parse_json(text)
        notes = {k: str(raw.get(k) or "") for k in ADS_NOTE_KEYS}
    except (AIError, ValueError, json.JSONDecodeError):
        return {"notes": ads_fallback_notes(report), "provider": None, "ai": False}
    AnalysisReport.objects.create(store=store, kind="ads", data_hash=h, period_days=days, provider=provider, content=notes)
    return {"notes": notes, "provider": provider, "ai": True}


def ads_chat(report: dict, messages: list[dict]) -> tuple[dict, str]:
    history = [{"role": m["role"], "content": str(m["content"])[:4000]}
               for m in messages[-10:] if m.get("role") in ("user", "assistant")]
    if not history or history[-1]["role"] != "user":
        raise AIError("Last message must come from the user")
    text, provider = complete(
        [{"role": "system", "content": ADS_CHAT_SYSTEM.format(snapshot=_dump(ads_snapshot(report)))}] + history, json_mode=True)
    try:
        out = _parse_json(text)
        answer = str(out.get("answer") or "").strip()
        follow = [str(f)[:80] for f in (out.get("follow_ups") or [])][:3]
        if not answer:
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        answer, follow = text.strip(), []
    return {"answer": answer, "follow_ups": follow}, provider


# ---- monitor: cards + morning brief ------------------------------------------------------------------------
SIGNALS_SYSTEM = """You are the profit analyst of an online store. The monitor found the SIGNALS below. Turn each into a
short, scannable card and write the morning brief. Use only the given numbers. Reply with JSON only:
{{"brief": {{"headline": "<=14 words: the state of profit today", "bullets": ["<=14 words each, exactly 3: what matters most"]}},
  "signals": [{{"id": "<id from input>", "headline": "<=8 words, plain, no jargon",
    "why": ["<=12 words each, 2 or 3 short reasons with a number"], "steps": ["imperative, <=14 words, 1 to 3 concrete steps"],
    "effort": "low|medium|high", "confidence": "low|medium|high"}}]}}
Rules: no field names, no markdown, no filler. Steps must be something the owner can do today in the shop or ad account.
effort = work needed, confidence = how sure the data makes you. Store currency: {currency}.

CONTEXT: {context}
SIGNALS: {signals}"""


def signals_ai(store: Store, data: dict, signals: list, health: dict) -> tuple[dict, dict, str]:
    t = data["totals"]
    ctx = {"period_days": data["period"]["days"], "revenue": round(t["revenue"]), "profit": round(t["profit"]), "margin_pct": t["margin_pct"],
           "ad_spend": round(t["ad_spend"]), "refund_rate_pct": t["refund_rate_pct"], "health_score": health["score"]}
    items = [{"id": s.fingerprint, "type": s.kind, "title": s.title, "detail": s.detail, "impact": float(s.impact), "evidence": s.viz}
             for s in signals]
    if not items:
        return {}, {}, ""
    text, provider = complete([{"role": "system", "content": SIGNALS_SYSTEM.format(
        currency=store.currency, context=json.dumps(ctx), signals=json.dumps(items, default=str, separators=(",", ":")))},
        {"role": "user", "content": "Write the cards and the brief."}], json_mode=True)
    try:
        raw = _parse_json(text)
    except (ValueError, json.JSONDecodeError):
        raise AIError("The model did not return valid JSON.")
    per = {}
    for it in raw.get("signals", []):
        if it.get("id") and it.get("headline"):
            per[it["id"]] = {
                "headline": str(it["headline"])[:90], "why": [str(w)[:110] for w in (it.get("why") or [])][:3],
                "steps": [str(x)[:130] for x in (it.get("steps") or [])][:3],
                "effort": it.get("effort") if it.get("effort") in ("low", "medium", "high") else "medium",
                "confidence": it.get("confidence") if it.get("confidence") in ("low", "medium", "high") else "medium"}
    b = raw.get("brief") or {}
    brief = {"headline": str(b.get("headline", ""))[:140], "bullets": [str(x)[:120] for x in (b.get("bullets") or [])][:3]}
    return brief, per, provider
