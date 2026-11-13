"""Connector contract.

Every data source (Shopify, Google Ads, Meta, TikTok, GA4, CSV, demo seed) implements `Connector`
and writes into the same normalised tables (Product / Order / OrderLine / AdCampaign / AdDaily).
The profit engine, insights and the AI only ever read those tables, so adding a source means
adding one file here and one line in REGISTRY, and nothing downstream changes.
"""
from dataclasses import dataclass, field

from core.models import Store


@dataclass
class SyncResult:
    products: int = 0
    orders: int = 0
    campaigns: int = 0
    ad_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    def message(self) -> str:
        parts = []
        if self.products:
            parts.append(f"{self.products} products")
        if self.orders:
            parts.append(f"{self.orders} orders")
        if self.campaigns:
            parts.append(f"{self.campaigns} campaigns / {self.ad_rows} daily ad rows")
        msg = "Synced " + (", ".join(parts) or "nothing new")
        if self.warnings:
            msg += " · " + " · ".join(self.warnings)
        return msg


class ConnectorError(Exception):
    """Raised with a message that is safe to show to the user."""


class Connector:
    name = "base"

    def __init__(self, store: Store):
        self.store = store

    def sync(self, days: int = 90) -> SyncResult:  # pragma: no cover - interface
        raise NotImplementedError


def get_connector(store: Store) -> Connector:
    from core.connectors.seed import DemoConnector
    from core.connectors.shopify import ShopifyConnector

    registry = {"shopify": ShopifyConnector, "demo": DemoConnector}
    # Future: "google_ads": GoogleAdsConnector, "meta": MetaConnector, "tiktok": ..., "ga4": ...
    try:
        return registry[store.platform](store)
    except KeyError:
        raise ConnectorError(f"No connector for platform '{store.platform}'")
