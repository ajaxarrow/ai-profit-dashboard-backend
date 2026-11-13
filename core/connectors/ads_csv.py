"""Import ad spend from CSV (Google Ads / Meta / TikTok exports) into any store.

Required columns: date, platform, campaign, spend
Optional: impressions, clicks, conversions, conversion_value, product_sku
This is the bridge until the live Google Ads / Meta API connectors exist.
"""
import csv
import io
from datetime import date
from decimal import Decimal, InvalidOperation

from django.db import transaction

from core.connectors.base import ConnectorError, SyncResult
from core.models import AdCampaign, AdDaily, Product, Store

TEMPLATE = (
    "date,platform,campaign,spend,impressions,clicks,conversions,conversion_value,product_sku\n"
    "2026-11-01,google,Google Shopping · Example,42.10,5200,180,3,240.00,SKU-1\n"
    "2026-11-01,meta,Meta · Prospecting,55.00,9100,210,2,130.00,\n"
)


def _num(v, default="0") -> Decimal:
    v = (v or "").strip().replace("'", "").replace(",", ".") or default
    try:
        return Decimal(v)
    except InvalidOperation:
        raise ConnectorError(f"Not a number: '{v}'")


@transaction.atomic
def import_ads(store: Store, raw: bytes) -> SyncResult:
    text = raw.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    missing = {"date", "platform", "campaign", "spend"} - {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    if missing:
        raise ConnectorError("CSV is missing columns: " + ", ".join(sorted(missing)))

    res = SyncResult()
    campaigns: dict = {}
    for i, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): v for k, v in row.items()}
        platform = (row["platform"] or "").strip().lower()
        if platform not in {"google", "meta", "tiktok"}:
            raise ConnectorError(f"Row {i}: platform must be google, meta or tiktok")
        try:
            d = date.fromisoformat(row["date"].strip())
        except ValueError:
            raise ConnectorError(f"Row {i}: date must be YYYY-MM-DD")
        name = row["campaign"].strip()
        key = (platform, name)
        if key not in campaigns:
            product = None
            sku = (row.get("product_sku") or "").strip()
            if sku:
                product = Product.objects.filter(store=store, sku=sku).first()
            campaigns[key], created = AdCampaign.objects.get_or_create(
                store=store, platform=platform, external_id=name, defaults={"name": name, "product": product},
            )
            res.campaigns += int(created)
        AdDaily.objects.update_or_create(
            campaign=campaigns[key], date=d,
            defaults={
                "spend": _num(row["spend"]), "impressions": int(_num(row.get("impressions"))),
                "clicks": int(_num(row.get("clicks"))), "conversions": _num(row.get("conversions")),
                "conversion_value": _num(row.get("conversion_value")),
            },
        )
        res.ad_rows += 1
    return res
