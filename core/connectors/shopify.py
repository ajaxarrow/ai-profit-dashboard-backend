"""Shopify Admin GraphQL connector.

Auth, either of:
  * a static Admin API access token (`shpat_...`, from a custom app), or
  * client id + secret from a Dev Dashboard app, exchanged for a short-lived token
    (client-credentials grant), which is how Shopify issues tokens for newly created apps.

Scopes required: read_products, read_orders, read_inventory (cost per item).
Add read_all_orders to sync orders older than 60 days.
"""
import re
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from core.connectors.base import Connector, ConnectorError, SyncResult
from core.models import Order, OrderLine, Product, Store

DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.myshopify\.com$")

SHOP_QUERY = "{ shop { name currencyCode } }"

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id title productType vendor
      variants(first: 25) { nodes { sku price inventoryItem { unitCost { amount } } } }
    }
  }
}"""

ORDERS_QUERY = """
query($cursor: String, $q: String) {
  orders(first: 10, after: $cursor, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id name createdAt processedAt cancelledAt sourceName tags
      currentTotalPriceSet { shopMoney { amount } }
      totalShippingPriceSet { shopMoney { amount } }
      totalTaxSet { shopMoney { amount } }
      lineItems(first: 20) {
        nodes {
          id title quantity sku
          originalUnitPriceSet { shopMoney { amount } }
          discountAllocations { allocatedAmountSet { shopMoney { amount } } }
          product { id }
          variant { inventoryItem { unitCost { amount } } }
        }
      }
      refunds { refundLineItems(first: 20) { nodes { quantity lineItem { id } } } }
    }
  }
}"""


def _gid(gid: str) -> str:
    return gid.rsplit("/", 1)[-1] if gid else ""


def _money(node) -> Decimal:
    try:
        return Decimal(str(node["shopMoney"]["amount"]))
    except (TypeError, KeyError):
        return Decimal("0")


def _source(node: dict) -> str:
    """Marketing channel from a `src:<channel>` order tag (set by seeding/imports), else Shopify's sourceName."""
    for t in node.get("tags") or []:
        if t.lower().startswith("src:"):
            return t[4:].strip().lower()[:40] or "online"
    return (node.get("sourceName") or "online")[:40]


def normalize_domain(raw: str) -> str:
    d = (raw or "").strip().lower().replace("https://", "").replace("http://", "").strip("/")
    if "." not in d:
        d = f"{d}.myshopify.com"
    if not DOMAIN_RE.match(d):
        raise ConnectorError("Use the shop's myshopify.com domain, e.g. my-shop.myshopify.com")
    return d


class ShopifyConnector(Connector):
    name = "shopify"

    # ---- low level -----------------------------------------------------------
    def _token(self) -> str:
        s = self.store
        if s.access_token:
            return s.access_token
        if s.client_id and s.client_secret:
            r = requests.post(
                f"https://{s.domain}/admin/oauth/access_token",
                json={"client_id": s.client_id, "client_secret": s.client_secret, "grant_type": "client_credentials"},
                timeout=30,
            )
            if r.status_code != 200:
                m = re.search(r"<title>\d+ - (.*?)</title>", r.text)
                reason = m.group(1) if m else (r.text[:120] or "no detail")
                hint = " Install the app on this store (same organization as the app) and try again." if "app_not_installed" in reason else ""
                raise ConnectorError(f"Shopify rejected the client credentials ({r.status_code}: {reason}).{hint}")
            return r.json()["access_token"]
        raise ConnectorError("No Shopify credentials stored for this store.")

    def _graphql(self, query: str, variables: dict | None = None, _retries: int = 4) -> dict:
        url = f"https://{self.store.domain}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
        headers = {"X-Shopify-Access-Token": self._cached_token, "Content-Type": "application/json"}
        for attempt in range(_retries):
            r = requests.post(url, json={"query": query, "variables": variables or {}}, headers=headers, timeout=60)
            if r.status_code == 401:
                raise ConnectorError("Shopify returned 401: the token is invalid or lacks scopes (need read_products, read_orders, read_inventory).")
            if r.status_code == 404:
                raise ConnectorError("Shop not found: check the myshopify.com domain.")
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            body = r.json()
            errors = body.get("errors")
            if errors:
                if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errors):
                    time.sleep(2 * (attempt + 1))
                    continue
                raise ConnectorError("Shopify GraphQL error: " + "; ".join(e.get("message", "?") for e in errors)[:300])
            return body["data"]
        raise ConnectorError("Shopify kept throttling the request. Try again in a minute.")

    def _paginate(self, query: str, field: str, variables: dict | None = None):
        cursor = None
        while True:
            data = self._graphql(query, {**(variables or {}), "cursor": cursor})[field]
            yield from data["nodes"]
            if not data["pageInfo"]["hasNextPage"]:
                return
            cursor = data["pageInfo"]["endCursor"]

    # ---- public ---------------------------------------------------------------
    def test_connection(self) -> dict:
        self._cached_token = self._token()
        return self._graphql(SHOP_QUERY)["shop"]

    def sync(self, days: int = 90) -> SyncResult:
        store = self.store
        res = SyncResult()
        self._cached_token = self._token()

        shop = self._graphql(SHOP_QUERY)["shop"]
        store.currency = shop["currencyCode"]
        if not store.name or store.name == store.domain:
            store.name = shop["name"]

        # Products first so order lines can link to them.
        product_map: dict[str, Product] = {}
        for node in self._paginate(PRODUCTS_QUERY, "products"):
            variants = node["variants"]["nodes"]
            price = Decimal(str(variants[0]["price"])) if variants else Decimal("0")
            costs = [
                Decimal(str(v["inventoryItem"]["unitCost"]["amount"]))
                for v in variants
                if v.get("inventoryItem") and v["inventoryItem"].get("unitCost")
            ]
            p, _ = Product.objects.update_or_create(
                store=store, external_id=_gid(node["id"]),
                defaults={
                    "title": node["title"][:300], "category": (node["productType"] or "Uncategorised")[:100],
                    "vendor": (node["vendor"] or "")[:100], "sku": (variants[0]["sku"] or "")[:100] if variants else "",
                    "price": price, "unit_cost": costs[0] if costs else None,
                },
            )
            product_map[p.external_id] = p
            res.products += 1

        since = (timezone.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        pct = Decimal(store.payment_fee_pct) / 100
        fixed = Decimal(store.payment_fee_fixed)
        for node in self._paginate(ORDERS_QUERY, "orders", {"q": f"processed_at:>={since}"}):
            self._save_order(node, product_map, pct, fixed)
            res.orders += 1

        if res.orders == 0:
            res.warnings.append("No orders in this window (old orders need the read_all_orders scope)")
        no_cost = sum(1 for p in product_map.values() if p.unit_cost is None)
        if no_cost:
            res.warnings.append(f"{no_cost} products have no 'cost per item', so profit is overstated for them")

        store.last_synced_at = timezone.now()
        store.last_sync_message = res.message()
        store.save()
        return res

    @transaction.atomic
    def _save_order(self, node: dict, product_map: dict, pct: Decimal, fixed: Decimal) -> None:
        store = self.store
        total = _money(node["currentTotalPriceSet"])
        order, _ = Order.objects.update_or_create(
            store=store, external_id=_gid(node["id"]),
            defaults={
                "name": node["name"], "created_at": parse_datetime(node.get("processedAt") or node["createdAt"]),
                "cancelled": bool(node.get("cancelledAt")), "source": _source(node),
                "shipping_charged": _money(node["totalShippingPriceSet"]),
                "shipping_cost": Decimal(store.default_shipping_cost),
                "tax": _money(node["totalTaxSet"]),
                "payment_fee": (total * pct + fixed).quantize(Decimal("0.01")),
            },
        )
        order.lines.all().delete()  # re-sync is idempotent
        refunded_by_line: dict[str, int] = {}
        for refund in node.get("refunds") or []:
            for rli in refund["refundLineItems"]["nodes"]:
                lid = _gid(rli["lineItem"]["id"])
                refunded_by_line[lid] = refunded_by_line.get(lid, 0) + rli["quantity"]

        lines = []
        for li in node["lineItems"]["nodes"]:
            qty = li["quantity"]
            discount = sum((_money(d["allocatedAmountSet"]) for d in li["discountAllocations"]), Decimal("0"))
            cost = None
            inv = (li.get("variant") or {}).get("inventoryItem")
            if inv and inv.get("unitCost"):
                cost = Decimal(str(inv["unitCost"]["amount"]))
            pid = _gid((li.get("product") or {}).get("id", ""))
            lines.append(OrderLine(
                order=order, product=product_map.get(pid), title=li["title"][:300], quantity=qty,
                unit_price=_money(li["originalUnitPriceSet"]), discount=discount, unit_cost=cost,
                refunded_quantity=min(qty, refunded_by_line.get(_gid(li["id"]), 0)),
            ))
        OrderLine.objects.bulk_create(lines)
