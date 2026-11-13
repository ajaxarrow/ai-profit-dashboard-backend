from decimal import Decimal, InvalidOperation

import requests
from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.response import Response

from core.ai import analyst
from core.ai.providers import AIError
from core.connectors import ads_csv
from core.connectors.base import ConnectorError, get_connector
from core.connectors.seed import load_demo
from core.connectors.shopify import ShopifyConnector, normalize_domain
from core.models import AnalysisReport, Signal, Store
from core.services import ads_export, ads_report, insights, monitor, profit

ASSUMPTION_FIELDS = ("payment_fee_pct", "payment_fee_fixed", "default_shipping_cost", "return_handling_cost")


def _store_json(s: Store) -> dict:
    return {
        "id": s.id, "name": s.name, "platform": s.platform, "domain": s.domain, "currency": s.currency,
        "last_synced_at": s.last_synced_at, "last_sync_message": s.last_sync_message,
        "has_credentials": bool(s.access_token or (s.client_id and s.client_secret)),
        "assumptions": {f: str(getattr(s, f)) for f in ASSUMPTION_FIELDS},
        "counts": {"products": s.products.count(), "orders": s.orders.count(), "campaigns": s.campaigns.count()},
    }


def _get_store(request) -> Store:
    sid = request.query_params.get("store") or request.data.get("store") if hasattr(request, "data") else None
    qs = Store.objects.all()
    # no store given: the live Shopify store wins over the demo data
    store = qs.filter(id=sid).first() if sid else (qs.filter(platform="shopify").order_by("id").first() or qs.order_by("id").first())
    if not store:
        raise ConnectorError("No store yet. Load the demo data or connect Shopify.")
    return store


def _days(request, default=30) -> int:
    try:
        return min(365, max(7, int(request.query_params.get("days", default))))
    except ValueError:
        return default


def _fail(msg, code=400):
    return Response({"error": str(msg)}, status=code)


@api_view(["GET"])
def stores(request):
    return Response([_store_json(s) for s in Store.objects.order_by("id")])


@api_view(["POST"])
def load_demo_store(request):
    store = Store.objects.filter(platform="demo").first() or Store.objects.create(
        name="Demo Spare Parts Shop", platform="demo", currency="CHF", domain="demo.local")
    res = load_demo(store)
    return Response({"store": _store_json(store), "message": res.message()})


@api_view(["POST"])
def connect_shopify(request):
    d = request.data
    try:
        domain = normalize_domain(d.get("domain", ""))
        token = (d.get("access_token") or "").strip()
        cid, secret = (d.get("client_id") or "").strip(), (d.get("client_secret") or "").strip()
        if not token and not (cid and secret):
            raise ConnectorError("Provide an Admin API access token, or a client id and secret.")
        store = Store.objects.filter(platform="shopify", domain=domain).first() or Store(platform="shopify", domain=domain, name=domain)
        store.access_token, store.client_id, store.client_secret = token, cid, secret
        shop = ShopifyConnector(store).test_connection()
        store.name, store.currency = shop["name"], shop["currencyCode"]
        store.save()
    except ConnectorError as e:
        return _fail(e)
    except requests.RequestException as e:
        return _fail(f"Could not reach Shopify: {e.__class__.__name__}", 502)
    return Response({"store": _store_json(store), "message": f"Connected to {store.name}. Run a sync to import data."})


@api_view(["POST"])
def sync_store(request, store_id):
    try:
        store = Store.objects.get(id=store_id)
        days = min(365, max(7, int(request.data.get("days", 90))))
        res = get_connector(store).sync(days)
    except Store.DoesNotExist:
        return _fail("Store not found", 404)
    except ConnectorError as e:
        return _fail(e)
    except requests.RequestException as e:
        return _fail(f"Could not reach the source: {e.__class__.__name__}", 502)
    return Response({"store": _store_json(store), "message": res.message()})


@api_view(["PATCH", "DELETE"])
def store_detail(request, store_id):
    try:
        store = Store.objects.get(id=store_id)
    except Store.DoesNotExist:
        return _fail("Store not found", 404)
    if request.method == "DELETE":
        store.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    try:
        for f in ASSUMPTION_FIELDS:
            if f in request.data:
                v = Decimal(str(request.data[f]))
                if v < 0:
                    raise InvalidOperation
                setattr(store, f, v)
    except InvalidOperation:
        return _fail("Assumptions must be non-negative numbers")
    store.save()
    return Response(_store_json(store))


@api_view(["POST"])
@parser_classes([MultiPartParser, JSONParser])
def import_ads(request, store_id):
    f = request.FILES.get("file")
    if not f:
        return _fail("Attach a CSV file as 'file'")
    try:
        store = Store.objects.get(id=store_id)
        res = ads_csv.import_ads(store, f.read())
    except Store.DoesNotExist:
        return _fail("Store not found", 404)
    except ConnectorError as e:
        return _fail(e)
    return Response({"store": _store_json(store), "message": res.message()})


@api_view(["GET"])
def ads_template(request):
    r = HttpResponse(ads_csv.TEMPLATE, content_type="text/csv")
    r["Content-Disposition"] = 'attachment; filename="ad-spend-template.csv"'
    return r


def _with_store(fn):
    def wrapper(request, *a, **kw):
        try:
            store = _get_store(request)
        except ConnectorError as e:
            return _fail(e, 404)
        return fn(request, store, *a, **kw)
    wrapper.__name__ = fn.__name__
    return wrapper


@api_view(["GET"])
@_with_store
def overview(request, store):
    data = profit.compute(store, _days(request))
    found = insights.find(data, store.currency)
    return Response({
        "store": _store_json(store), "period": data["period"], "totals": data["totals"],
        "previous": data["previous"], "series": data["series"], "channels": data["channels"],
        "categories": data["categories"], "ad_spend_by_platform": data["ad_spend_by_platform"],
        "top_products": data["products"][:5], "worst_products": data["products"][-5:][::-1],
        "campaigns": [{k: c[k] for k in ("id", "platform", "name", "spend", "roas", "profit_after_ads")} for c in data["campaigns"]],
        "findings": found[:4], "findings_total": len(found),
    })


@api_view(["GET"])
@_with_store
def products(request, store):
    data = profit.compute(store, _days(request), include_previous=False)
    return Response({"period": data["period"], "currency": store.currency, "products": data["products"]})


@api_view(["GET"])
@_with_store
def campaigns(request, store):
    data = profit.compute(store, _days(request), include_previous=False)
    return Response({"period": data["period"], "currency": store.currency, "campaigns": data["campaigns"],
                     "totals": data["totals"], "by_platform": data["ad_spend_by_platform"]})


@api_view(["GET"])
@_with_store
def findings(request, store):
    data = profit.compute(store, _days(request), include_previous=False)
    return Response({"period": data["period"], "currency": store.currency, "findings": insights.find(data, store.currency)})


def _report_json(rep: AnalysisReport, current_hash: str, days: int) -> dict:
    return {"id": rep.id, "created_at": rep.created_at, "provider": rep.provider, "period_days": rep.period_days,
            "stale": rep.data_hash != current_hash, "other_period": rep.period_days != days, **rep.content}


@api_view(["GET", "POST"])
@_with_store
def analysis(request, store):
    """GET returns the saved review (this period if there is one, else the latest for the store). POST re-runs it."""
    days = _days(request)
    current = analyst.data_hash(profit.compute(store, days))
    if request.method == "POST":
        try:
            rep = analyst.analyse(store, days)
        except AIError as e:
            return _fail(e, 502)
    else:
        reports = AnalysisReport.objects.filter(store=store, kind="review")
        rep = reports.filter(period_days=days).first() or reports.first()
        if not rep:
            return Response({"report": None})
    return Response({"report": _report_json(rep, current, days)})


@api_view(["GET", "POST"])
@_with_store
def chart_notes(request, store):
    """Short AI commentary for every chart. Cached per store/period/data; POST forces a rewrite."""
    return Response(analyst.chart_notes(store, _days(request), force=request.method == "POST"))


@api_view(["POST"])
@_with_store
def chat(request, store):
    msgs = request.data.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return _fail("messages must be a non-empty list")
    try:
        if request.query_params.get("scope") == "ads":
            report = ads_report.compute(store, _days(request))
            if not report["has_ads"]:
                return _fail("This store has no ad data yet. Import ad spend on Data sources first.")
            out, provider = analyst.ads_chat(report, msgs)
        else:
            out, provider = analyst.chat(store, msgs, _days(request))
    except AIError as e:
        return _fail(e, 502)
    return Response({"reply": out["answer"], "follow_ups": out["follow_ups"], "provider": provider})


@api_view(["GET", "POST"])
@_with_store
def ads_report_view(request, store):
    """Comprehensive advertising report. AI commentary is attached when `notes=1` (cached; POST rewrites it)."""
    days = _days(request)
    report = ads_report.compute(store, days)
    if report["has_ads"] and (request.query_params.get("notes") == "1" or request.method == "POST"):
        report["ai"] = analyst.ads_notes(store, days, report, force=request.method == "POST")
    return Response(report)


@api_view(["GET"])
@_with_store
def ads_report_export(request, store):
    days = _days(request)
    report = ads_report.compute(store, days)
    if not report["has_ads"]:
        return _fail("No ad data to export.", 404)
    fmt = request.query_params.get("fmt", "xlsx")
    stamp = f"{report['period']['start']}_to_{report['period']['end']}"
    base = f"advertising-report_{stamp}"
    if fmt == "xlsx":
        ai = analyst.ads_notes(store, days, report)
        body = ads_export.to_xlsx(report, store.name, ai["notes"] or None)
        resp = HttpResponse(body, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{base}.xlsx"'
        return resp
    which = request.query_params.get("table", "campaigns")
    if which not in ("campaigns", "platforms", "daily", "correlations"):
        return _fail("Unknown table")
    resp = HttpResponse(ads_export.to_csv(report, which), content_type="text/csv")
    resp["Content-Disposition"] = f'attachment; filename="{base}_{which}.csv"'
    return resp


@api_view(["GET", "POST"])
@_with_store
def command(request, store):
    """GET: the Command Center (latest monitor run + signals). POST: run the monitor now."""
    if request.method == "POST":
        try:
            monitor.run(store, _days(request), trigger="manual")
        except AIError as e:
            return _fail(e, 502)
    snap = monitor.snapshot(store)
    return Response({"snapshot": snap, "currency": store.currency})


@api_view(["PATCH"])
def signal_detail(request, signal_id):
    try:
        s = Signal.objects.get(id=signal_id)
    except Signal.DoesNotExist:
        return _fail("Signal not found", 404)
    new = request.data.get("status")
    if new not in ("new", "approved", "snoozed", "dismissed"):
        return _fail("Unknown status")
    from django.utils import timezone
    s.status = new
    if new == "approved":
        s.approved_at, s.approved_impact = timezone.now(), s.impact
    elif new == "new":
        s.approved_at, s.approved_impact = None, None
    s.save()
    return Response(monitor.serialize_signal(s))


@api_view(["GET"])
def app_config(request):
    from django.conf import settings
    return Response({"read_only": settings.READ_ONLY, "as_of": settings.PROFIT_AS_OF or None})
