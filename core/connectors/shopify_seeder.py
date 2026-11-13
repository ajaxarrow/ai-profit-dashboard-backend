"""Push demo products and a backdated order history INTO a real Shopify (dev) store.

Uses the same product catalogue and "stories" as the local demo seed, so the dashboard shows the same
patterns on live Shopify data. Everything created is tagged `profitdesk-seed`.

App scopes needed (in addition to the read scopes): write_products, write_orders, write_inventory.
Notes:
  * Backdated via `processedAt`; Shopify keeps `createdAt` = now, so the connector reads processedAt.
  * Channel is stored as an order tag `src:<channel>` (sourceName only accepts registered attribution handles).
  * Discounts are order-level percentages (the API's percentage code applies to every line).
  * Dev stores may throttle order creation; rate-limit errors are retried after a pause.
"""
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from core.connectors.base import ConnectorError
from core.connectors.seed import PRODUCTS, SOURCES, _weighted
from core.connectors.shopify import ShopifyConnector, _gid

TAG = "profitdesk-seed"
D = Decimal

PRODUCT_SET = """
mutation($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id variants(first: 1) { nodes { id sku } } }
    userErrors { field message }
  }
}"""

ORDER_CREATE = """
mutation($order: OrderCreateOrderInput!) {
  orderCreate(order: $order, options: { inventoryBehaviour: BYPASS, sendReceipt: false, sendFulfillmentReceipt: false }) {
    order { id name lineItems(first: 10) { nodes { id variant { sku } } } transactions { id } }
    userErrors { field message }
  }
}"""

REFUND_CREATE = """
mutation($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) {
    refund { id }
    userErrors { field message }
  }
}"""

EXISTING_PRODUCTS = """
query($cursor: String) {
  products(first: 100, after: $cursor, query: "tag:%s") {
    pageInfo { hasNextPage endCursor }
    nodes { id variants(first: 1) { nodes { id sku } } }
  }
}""" % TAG

EXISTING_ORDERS = '{ orders(first: 1, query: "tag:%s") { nodes { id } } }' % TAG
SHOP = "{ shop { currencyCode } }"

RATE_LIMIT_RE = re.compile(r"limit|too many|throttl|try again", re.I)


def _q(x) -> Decimal:
    return D(str(x)).quantize(D("0.01"), rounding=ROUND_HALF_UP)


# ---- pure: build what we are going to create -------------------------------------------------
def build_orders(total: int, days: int, seed: int = 42, now: datetime | None = None) -> list[dict]:
    """Deterministic order specs with the same story weights as the local demo seed."""
    rng = random.Random(seed)
    now = now or datetime.now(dt_timezone.utc)
    meta = {row[1]: row for row in PRODUCTS}
    weights = [(row[1], row[5]) for row in PRODUCTS]

    day_list, day_w = [], []
    for back in range(days - 1, -1, -1):
        day = (now - timedelta(days=back)).date()
        growth = 0.85 + 0.3 * (days - back) / days
        day_list.append(day)
        day_w.append(growth * [1.1, 1.05, 1.0, 1.0, 1.05, 0.85, 0.8][day.weekday()])

    specs = []
    for i in range(total):
        day = rng.choices(day_list, day_w)[0]
        ts = datetime.combine(day, datetime.min.time(), tzinfo=dt_timezone.utc) + timedelta(seconds=rng.randint(8 * 3600, 22 * 3600))
        if ts > now:
            ts = now - timedelta(minutes=rng.randint(5, 600))
        skus = []
        for _ in range(_weighted(rng, [(1, 62), (2, 28), (3, 10)])):
            s = _weighted(rng, weights)
            if s not in skus:
                skus.append(s)
        lines, max_pct = [], 0.0
        for sku in skus:
            _, _, _, price, _, _, dprob, dpct, rprob = meta[sku]
            qty = 2 if rng.random() < 0.12 else 1
            refunded = sum(1 for _ in range(qty) if rng.random() < rprob)
            lines.append({"sku": sku, "quantity": qty, "price": price, "refunded": refunded})
            if rng.random() < dprob:
                max_pct = max(max_pct, round(dpct * rng.uniform(0.8, 1.2) * 100, 1))
        subtotal = sum(D(str(l["price"])) * l["quantity"] for l in lines)
        specs.append({
            "key": f"{seed}-{i}", "processed_at": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "lines": lines,
            "discount_pct": max_pct or None, "source": _weighted(rng, SOURCES),
            "shipping": D("6.90") if subtotal < 100 else D("0"),
        })
    specs.sort(key=lambda s: s["processed_at"])
    return specs


def order_input(spec: dict, variant_ids: dict, currency: str) -> dict:
    money = lambda v: {"shopMoney": {"amount": str(_q(v)), "currencyCode": currency}}
    pct = D(str(spec["discount_pct"] or 0))
    items = sum(D(str(l["price"])) * l["quantity"] for l in spec["lines"])
    discount = sum(_q(D(str(l["price"])) * l["quantity"] * pct / 100) for l in spec["lines"])
    total = items - discount + spec["shipping"]
    order = {
        "processedAt": spec["processed_at"], "currency": currency, "financialStatus": "PAID",
        "tags": [TAG, f"src:{spec['source']}"],
        "lineItems": [{"variantId": variant_ids[l["sku"]], "quantity": l["quantity"]} for l in spec["lines"]],
        "transactions": [{"kind": "SALE", "status": "SUCCESS", "gateway": "manual", "amountSet": money(total)}],
    }
    if pct:
        order["discountCode"] = {"itemPercentageDiscountCode": {"code": f"SEED{int(pct)}", "percentage": float(pct)}}
    if spec["shipping"]:
        order["shippingLines"] = [{"title": "Standard shipping", "priceSet": money(spec["shipping"])}]
    return order


# ---- API side ----------------------------------------------------------------------------------
class ShopifySeeder:
    def __init__(self, connector: ShopifyConnector, log=print):
        self.c = connector
        self.log = log

    def _call(self, query, variables, root):
        """Run a mutation; retry on rate-limit style user errors."""
        for attempt in range(6):
            data = self.c._graphql(query, variables)[root]
            errs = data.get("userErrors") or []
            if not errs:
                return data
            msg = "; ".join(e["message"] for e in errs)
            if RATE_LIMIT_RE.search(msg) and attempt < 5:
                self.log(f"   rate limited ({msg[:80]}), waiting 60s…")
                time.sleep(60)
                continue
            raise ConnectorError(f"{root}: {msg}"[:400])
        raise ConnectorError(f"{root}: still rate limited")

    def run(self, orders: int, days: int, seed: int = 42, pace: float = 0, force: bool = False) -> dict:
        c = self.c
        c._cached_token = c._token()
        currency = c._graphql(SHOP)["shop"]["currencyCode"]
        if not force and c._graphql(EXISTING_ORDERS)["orders"]["nodes"]:
            raise ConnectorError(f"This store already has orders tagged '{TAG}'. Pass --force to add more.")

        variant_ids = self._ensure_products()
        specs = build_orders(orders, days, seed)
        random.Random(seed).shuffle(specs)  # random date order: stopping early still leaves history across the whole period
        created = refunded = 0
        for n, spec in enumerate(specs, 1):
            res = self._call(ORDER_CREATE, {"order": order_input(spec, variant_ids, currency)}, "orderCreate")["order"]
            created += 1
            to_refund = [l for l in spec["lines"] if l["refunded"]]
            if to_refund:
                by_sku = {li["variant"]["sku"]: li["id"] for li in res["lineItems"]["nodes"] if li.get("variant")}
                pct = D(str(spec["discount_pct"] or 0))
                amount = sum(_q(D(str(l["price"])) * l["refunded"] * (1 - pct / 100)) for l in to_refund)
                oid = res["id"]
                self._call(REFUND_CREATE, {
                    "key": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{TAG}/{spec['key']}")),
                    "input": {
                        "orderId": oid,
                        "refundLineItems": [{"lineItemId": by_sku[l["sku"]], "quantity": l["refunded"]} for l in to_refund],
                        "transactions": [{"orderId": oid, "parentId": res["transactions"][0]["id"], "kind": "REFUND",
                                          "gateway": "manual", "amount": str(amount)}],
                    },
                }, "refundCreate")
                refunded += 1
            if n % 10 == 0 or n == len(specs):
                self.log(f"   {n}/{len(specs)} orders ({refunded} with refunds)")
            if pace:
                time.sleep(pace)
        return {"products": len(variant_ids), "orders": created, "refunds": refunded, "currency": currency}

    def _ensure_products(self) -> dict:
        """sku -> variant gid. Creates only what is missing (matched by SKU among seed-tagged products)."""
        have = {}
        for node in self.c._paginate(EXISTING_PRODUCTS, "products"):
            for v in node["variants"]["nodes"]:
                have[v["sku"]] = v["id"]
        made = 0
        for title, sku, cat, price, cost, *_ in PRODUCTS:
            if sku in have:
                continue
            variant = {
                "optionValues": [{"optionName": "Title", "name": "Default Title"}],
                "price": str(_q(price)), "sku": sku,
                "inventoryItem": {"tracked": False, **({"cost": str(_q(cost))} if cost is not None else {})},
            }
            data = self._call(PRODUCT_SET, {"input": {
                "title": title, "productType": cat, "vendor": "OEM compatible", "status": "ACTIVE", "tags": [TAG],
                "productOptions": [{"name": "Title", "position": 1, "values": [{"name": "Default Title"}]}],
                "variants": [variant],
            }}, "productSet")
            have[sku] = data["product"]["variants"]["nodes"][0]["id"]
            made += 1
        self.log(f"   products: {made} created, {len(have) - made} already there")
        return have
