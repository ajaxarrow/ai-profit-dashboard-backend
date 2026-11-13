"""Deterministic findings. These are the evidence; the AI layer explains and prioritises them.

Each finding: id, kind, severity (high|medium|low), title, detail, impact (money at stake over the
period, store currency; positive = recoverable/gainable), entity, action.
"""


def _sev(impact: float, revenue: float) -> str:
    share = impact / revenue if revenue else 0
    if impact >= 1500 or share >= 0.02:
        return "high"
    if impact >= 400 or share >= 0.005:
        return "medium"
    return "low"


def find(data: dict, currency: str = "CHF") -> list[dict]:
    t = data["totals"]
    revenue = t["revenue"] or 0
    out: list[dict] = []
    n = 0

    def add(kind, severity, title, detail, impact, entity, action):
        nonlocal n
        n += 1
        out.append({
            "id": f"f{n}", "kind": kind, "severity": severity, "title": title, "detail": detail,
            "impact": round(impact, 2), "entity": entity, "action": action,
        })

    products = data["products"]
    # A campaign needs enough spend to be judged. Scales with the store so small shops still get findings.
    min_spend = max(10.0, min(100.0, 0.03 * t["ad_spend"]))

    # 1. Products that lose money after every cost
    for p in products:
        if p["profit"] < 0 and p["revenue"] > 0:
            loss = -p["profit"]
            reasons = []
            if p["refund_rate_pct"] >= 12:
                reasons.append(f"{p['refund_rate_pct']}% of units refunded")
            if p["discount_rate_pct"] >= 15:
                reasons.append(f"{p['discount_rate_pct']}% average discount")
            if p["ad_spend"] > max(0, p["contribution"]):
                reasons.append(f"{currency} {p['ad_spend']:,.0f} ad spend exceeds its contribution")
            if p["cogs"] > 0 and p["revenue"] > 0 and p["cogs"] / p["revenue"] > 0.7:
                reasons.append("cost of goods is above 70% of revenue")
            why = "; ".join(reasons) or "costs exceed revenue"
            add("loss_product", _sev(loss, revenue),
                f"{p['title']} loses {currency} {loss:,.0f}",
                f"Revenue {currency} {p['revenue']:,.0f}, profit {'−' if p['profit'] < 0 else ''}{currency} {abs(p['profit']):,.0f} over {data['period']['days']} days. Why: {why}.",
                loss, p["title"],
                "Raise the price, cut the discount or stop advertising it until the margin is positive.")

    # 2. High-return products that are still profitable but leaking
    for p in products:
        if p["profit"] >= 0 and p["refund_rate_pct"] >= 15 and p["units"] >= 15:
            leak = p["refunds"] * 0.5 + p["return_cost"]
            add("returns", _sev(leak, revenue),
                f"{p['title']}: {p['refund_rate_pct']}% of units come back",
                f"{p['refunded_units']} of {p['units']} units refunded, costing {currency} {p['return_cost']:,.0f} in return handling plus lost margin.",
                leak, p["title"],
                "Check product description, fitment info and photos; for spare parts add a compatibility check by model number.")

    # 3. Campaigns that cost more than the margin they earn
    for c in data["campaigns"]:
        if c["spend"] < min_spend:
            continue
        if c["profit_after_ads"] < 0:
            loss = -c["profit_after_ads"]
            be = c["break_even_roas"]
            add("loss_campaign", _sev(loss, revenue),
                f"{c['name']} burns {currency} {loss:,.0f}",
                f"{c['platform'].title()} reports ROAS {c['roas']}, but " + (f"at a {c['margin_ratio_pct']}% margin it needs {be} to break even" if be else "the product it promotes has a negative margin, so no ROAS can break even") + f". Spend {currency} {c['spend']:,.0f}, platform-reported sales {currency} {c['conversion_value']:,.0f}.",
                loss, c["name"],
                "Cut the budget, tighten audiences/keywords, or fix the landing page. Re-test with a lower daily cap.")

    # 4. Campaigns with headroom to scale
    for c in data["campaigns"]:
        be = c["break_even_roas"]
        if c["spend"] >= min_spend and be and c["roas"] and c["roas"] >= be * 1.8 and c["profit_after_ads"] > 0:
            gain = c["profit_after_ads"] * 0.5
            add("scale_campaign", "medium" if gain > 300 else "low",
                f"{c['name']} is under-funded",
                f"ROAS {c['roas']} vs break-even {be}: it earned {currency} {c['profit_after_ads']:,.0f} after ad cost on {currency} {c['spend']:,.0f} spend.",
                gain, c["name"],
                "Raise the budget 20-30% and watch ROAS for a week; stop if it drops under 1.5x break-even.")

    # 5. Heavy discounting that is not buying volume
    heavy = [p for p in products if p["discount_rate_pct"] >= 25 and p["profit"] >= 0 and p["revenue"] > 500]
    for p in sorted(heavy, key=lambda p: -p["discounts"])[:3]:
        add("discount", _sev(p["discounts"] * 0.3, revenue),
            f"{p['title']} is discounted {p['discount_rate_pct']}% on average",
            f"{currency} {p['discounts']:,.0f} given away; margin is {p['margin_pct']}%.",
            p["discounts"] * 0.3, p["title"],
            "Reduce the discount depth or limit it to a code/segment instead of the whole catalogue.")

    # 6. Data quality: profit is overstated when costs are missing
    missing = data.get("missing_cost_products") or []
    if missing:
        add("data_quality", "high" if len(missing) >= 5 else "medium",
            f"{len(missing)} product(s) have no cost price",
            f"Profit for these is overstated because COGS counts as zero: {', '.join(missing[:5])}{'…' if len(missing) > 5 else ''}.",
            0.0, f"{len(missing)} products",
            "Add 'Cost per item' in Shopify (Products → Variant → Cost per item) and re-sync.")

    # 7. Store-level: ad spend that cannot be tied to a product and a thin overall margin
    if t["ad_spend"] and t["mer"] is not None and t["contribution_margin_pct"]:
        be_mer = 100 / t["contribution_margin_pct"]
        if t["mer"] < be_mer:
            gap = t["ad_spend"] - t["contribution"]
            add("store_mer", "high",
                f"Total ad spend is not paying for itself (MER {t['mer']} vs break-even {be_mer:.1f})",
                f"{currency} {t['ad_spend']:,.0f} of ads against {currency} {t['contribution']:,.0f} contribution before ads.",
                max(gap, 0), "All channels",
                "Re-allocate budget from the loss-making campaigns first; see the campaign findings.")

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda f: (order[f["severity"]], -f["impact"]))
    for i, f in enumerate(out, 1):
        f["id"] = f"f{i}"
    return out
