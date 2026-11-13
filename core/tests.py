from datetime import timedelta
from decimal import Decimal as D
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core.connectors.shopify import ShopifyConnector, normalize_domain
from core.connectors.base import ConnectorError
from core.models import AdCampaign, AdDaily, Order, OrderLine, Product, Store
from core.services import insights, profit


class ProfitMathTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="t", platform="demo", return_handling_cost=D("5"))
        self.p = Product.objects.create(store=self.store, external_id="1", title="Pump", sku="P", unit_cost=D("40"), price=D("100"))
        o = Order.objects.create(
            store=self.store, external_id="o1", created_at=timezone.now() - timedelta(days=1),
            shipping_charged=D("5"), shipping_cost=D("8"), payment_fee=D("3"),
        )
        # 2 units @100, 20 discount, 1 refunded -> net 180, refund 90, cogs 40, fee 3, ship 3, return 5
        OrderLine.objects.create(order=o, product=self.p, quantity=2, unit_price=D("100"), discount=D("20"),
                                 unit_cost=D("40"), refunded_quantity=1)

    def test_product_profit_after_all_costs(self):
        data = profit.compute(self.store, 7, include_previous=False)
        p = data["products"][0]
        self.assertEqual(p["net_revenue"], 180)
        self.assertEqual(p["refunds"], 90)
        self.assertEqual(p["revenue"], 90)
        self.assertEqual(p["cogs"], 40)
        self.assertEqual(p["contribution"], 90 - 40 - 3 - 3 - 5)  # 39
        self.assertEqual(data["totals"]["profit"], 39)

    def test_ad_spend_lands_on_linked_product_and_store(self):
        c = AdCampaign.objects.create(store=self.store, platform="google", external_id="c", name="Shop", product=self.p)
        AdDaily.objects.create(campaign=c, date=(timezone.now() - timedelta(days=1)).date(), spend=D("30"), conversion_value=D("100"))
        data = profit.compute(self.store, 7, include_previous=False)
        self.assertEqual(data["products"][0]["ad_spend"], 30)
        self.assertEqual(data["totals"]["profit"], 9)
        self.assertEqual(data["totals"]["ad_spend_unattributed"], 0)

    def test_missing_cost_is_flagged(self):
        self.p.unit_cost = None
        self.p.save()
        OrderLine.objects.update(unit_cost=None)
        data = profit.compute(self.store, 7, include_previous=False)
        kinds = [f["kind"] for f in insights.find(data)]
        self.assertIn("data_quality", kinds)

    def test_cancelled_orders_ignored(self):
        Order.objects.update(cancelled=True)
        self.assertEqual(profit.compute(self.store, 7, include_previous=False)["totals"]["orders"], 0)


class ShopifyMappingTests(TestCase):
    def test_domain_validation(self):
        self.assertEqual(normalize_domain("https://My-Shop.myshopify.com/"), "my-shop.myshopify.com")
        self.assertEqual(normalize_domain("my-shop"), "my-shop.myshopify.com")
        with self.assertRaises(ConnectorError):
            normalize_domain("evil.com")

    def test_sync_maps_orders_refunds_and_costs(self):
        store = Store.objects.create(name="s", platform="shopify", domain="s.myshopify.com", access_token="x")
        money = lambda a: {"shopMoney": {"amount": a}}
        product = {"id": "gid://shopify/Product/1", "title": "Pump", "productType": "Dishwasher", "vendor": "V",
                   "variants": {"nodes": [{"sku": "P", "price": "50.00", "inventoryItem": {"unitCost": {"amount": "20.00"}}}]}}
        order = {
            "id": "gid://shopify/Order/9", "name": "#1009", "createdAt": timezone.now().isoformat(), "cancelledAt": None,
            "sourceName": "web", "currentTotalPriceSet": money("100"), "totalShippingPriceSet": money("5"), "totalTaxSet": money("7"),
            "lineItems": {"nodes": [{
                "id": "gid://shopify/LineItem/1", "title": "Pump", "quantity": 2, "sku": "P",
                "originalUnitPriceSet": money("50.00"),
                "discountAllocations": [{"allocatedAmountSet": money("10.00")}],
                "product": {"id": "gid://shopify/Product/1"},
                "variant": {"inventoryItem": {"unitCost": {"amount": "20.00"}}},
            }]},
            "refunds": [{"refundLineItems": {"nodes": [{"quantity": 1, "lineItem": {"id": "gid://shopify/LineItem/1"}}]}}],
        }
        page = lambda nodes: {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

        def fake(self_, query, variables=None, _retries=4):
            if "currencyCode" in query:
                return {"shop": {"name": "Shop", "currencyCode": "CHF"}}
            if "products(" in query:
                return {"products": page([product])}
            return {"orders": page([order])}

        with mock.patch.object(ShopifyConnector, "_graphql", fake):
            res = ShopifyConnector(store).sync(30)
        self.assertEqual((res.products, res.orders), (1, 1))
        line = OrderLine.objects.get()
        self.assertEqual((line.discount, line.unit_cost, line.refunded_quantity), (D("10.00"), D("20.00"), 1))
        o = Order.objects.get()
        self.assertEqual(o.payment_fee, D("3.20"))  # 2.9% of 100 + 0.30
        store.refresh_from_db()
        self.assertEqual(store.currency, "CHF")


class ShopifySeederTests(TestCase):
    def test_build_orders_is_deterministic_and_historical(self):
        from datetime import datetime, timezone as tz
        from core.connectors.shopify_seeder import build_orders
        now = datetime(2026, 11, 13, 12, 0, tzinfo=tz.utc)
        a, b = build_orders(120, 90, 7, now), build_orders(120, 90, 7, now)
        self.assertEqual(a, b)
        self.assertEqual(len(a), 120)
        self.assertTrue(all(s["processed_at"] <= "2026-11-13T12:00:00Z" for s in a))
        self.assertGreater(len({s["processed_at"][:10] for s in a}), 40)  # spread over history
        self.assertTrue(any(l["refunded"] for s in a for l in s["lines"]))
        self.assertTrue(any(s["discount_pct"] for s in a))

    def test_order_input_total_matches_transaction(self):
        from decimal import Decimal as D
        from core.connectors.shopify_seeder import order_input
        spec = {"processed_at": "2026-10-01T10:00:00Z", "source": "meta", "discount_pct": 30.0, "shipping": D("6.90"),
                "lines": [{"sku": "A", "quantity": 1, "price": 50.0, "refunded": 0}]}
        o = order_input(spec, {"A": "gid://shopify/ProductVariant/1"}, "CHF")
        self.assertEqual(o["transactions"][0]["amountSet"]["shopMoney"]["amount"], "41.90")  # 50 - 15 + 6.90
        self.assertIn("src:meta", o["tags"])
        self.assertEqual(o["discountCode"]["itemPercentageDiscountCode"]["percentage"], 30.0)

    def test_run_creates_products_orders_and_refunds(self):
        from core.connectors.shopify_seeder import PRODUCTS, ShopifySeeder
        store = Store.objects.create(name="s", platform="shopify", domain="s.myshopify.com", access_token="x")
        calls = []

        def fake(self_, query, variables=None, _retries=4):
            calls.append(query.split("(")[0].split("{")[0].strip() or query[:30])
            if "currencyCode" in query:
                return {"shop": {"currencyCode": "CHF"}}
            if "tag:profitdesk-seed" in query and "orders(" in query:
                return {"orders": {"nodes": []}}
            if "products(" in query:
                return {"products": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}
            if "productSet" in query:
                sku = variables["input"]["variants"][0]["sku"]
                return {"productSet": {"product": {"id": "p", "variants": {"nodes": [{"id": f"gid://v/{sku}", "sku": sku}]}}, "userErrors": []}}
            if "orderCreate" in query:
                skus = [l["variantId"].rsplit("/", 1)[-1] for l in variables["order"]["lineItems"]]
                nodes = [{"id": f"li-{s}", "variant": {"sku": s}} for s in skus]
                return {"orderCreate": {"order": {"id": "o1", "name": "#1", "lineItems": {"nodes": nodes}, "transactions": [{"id": "t1"}]}, "userErrors": []}}
            if "refundCreate" in query:
                self.assertEqual(variables["input"]["transactions"][0]["kind"], "REFUND")
                return {"refundCreate": {"refund": {"id": "r"}, "userErrors": []}}
            raise AssertionError(query)

        with mock.patch.object(ShopifyConnector, "_graphql", fake), mock.patch.object(ShopifyConnector, "_token", lambda s: "x"):
            res = ShopifySeeder(ShopifyConnector(store), log=lambda m: None).run(60, 30, 3)
        self.assertEqual((res["products"], res["orders"]), (len(PRODUCTS), 60))
        self.assertGreater(res["refunds"], 0)

    def test_sync_prefers_src_tag_and_processed_at(self):
        from core.connectors.shopify import _source
        self.assertEqual(_source({"tags": ["profitdesk-seed", "src:Google"], "sourceName": "web"}), "google")
        self.assertEqual(_source({"tags": [], "sourceName": "web"}), "web")


class AdsReportTests(TestCase):
    def test_pearson_and_weekday_adjustment(self):
        from datetime import date, timedelta
        from core.services.ads_report import _deseason, pearson
        xs = [1, 2, 3, 4, 5, 6, 7, 8.0]
        self.assertAlmostEqual(pearson(xs, [2 * x for x in xs]), 1.0)
        self.assertAlmostEqual(pearson(xs, [-x for x in xs]), -1.0)
        self.assertIsNone(pearson([1, 1, 1, 1, 1, 1, 1, 1.0], xs))  # constant series has no correlation
        self.assertIsNone(pearson(xs[:5], xs[:5]))  # too few points
        # A pure weekly pattern shared by two series: strongly correlated raw, ~nothing once the pattern is removed
        days = [date(2026, 1, 5) + timedelta(days=i) for i in range(28)]
        rhythm = [1.2, 1.0, 1.0, 1.0, 1.1, 0.8, 0.7]
        a = [100 * rhythm[d.weekday()] for d in days]
        b = [10 * rhythm[d.weekday()] + (i % 3) * 0.01 for i, d in enumerate(days)]
        self.assertGreater(pearson(a, b), 0.95)
        adj = pearson(_deseason(a, days), _deseason(b, days))
        self.assertTrue(adj is None or abs(adj) < 0.3)

    def test_report_and_exports_on_seeded_data(self):
        from core.connectors.seed import load_demo
        from core.services import ads_export, ads_report
        store = Store.objects.create(name="demo", platform="demo", currency="CHF")
        load_demo(store, days=60)
        rep = ads_report.compute(store, 30)
        self.assertTrue(rep["has_ads"])
        self.assertEqual(len(rep["daily"]), 30)
        self.assertAlmostEqual(sum(p["spend"] for p in rep["platforms"]), rep["kpis"]["spend"], places=0)
        self.assertTrue(any(c["label"] == "Ad spend vs revenue" for c in rep["correlations"]))
        self.assertIn("Campaign", ads_export.to_csv(rep, "campaigns").splitlines()[0])
        import io, openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(ads_export.to_xlsx(rep, "demo", {"summary": "x"})))
        self.assertEqual(wb.sheetnames, ["Summary", "Platforms", "Campaigns", "Daily", "Weekly", "Correlations"])

    def test_store_without_ads_reports_has_ads_false(self):
        from core.services import ads_report
        store = Store.objects.create(name="empty", platform="demo")
        self.assertFalse(ads_report.compute(store, 30)["has_ads"])


class MonitorTests(TestCase):
    def setUp(self):
        from core.connectors.seed import load_demo
        self.store = Store.objects.create(name="demo", platform="demo", currency="CHF")
        load_demo(self.store, days=60)

    def _run(self):
        from core.ai.providers import AIError
        from core.services import monitor
        with mock.patch("core.ai.analyst.signals_ai", side_effect=AIError("offline")):  # deterministic fallback path
            return monitor.run(self.store, 30)

    def test_run_creates_signals_health_and_brief_without_ai(self):
        from core.models import Signal
        run = self._run()
        self.assertGreater(Signal.objects.filter(store=self.store).count(), 3)
        self.assertTrue(0 <= run.health["score"] <= 100)
        self.assertTrue(run.brief["headline"])
        kinds = set(Signal.objects.values_list("kind", flat=True))
        self.assertIn("loss_campaign", kinds)
        self.assertTrue(all(s.ai.get("steps") for s in Signal.objects.all()))  # never an empty card

    def test_rerun_is_stable_and_approved_problem_that_disappears_is_a_win(self):
        from core.models import AdCampaign, Signal
        from core.services import monitor
        self._run()
        n = Signal.objects.count()
        self._run()
        self.assertEqual(Signal.objects.count(), n)  # same problems keep their identity, no duplicates
        sig = Signal.objects.filter(kind="loss_campaign").first()
        sig.status = "approved"; sig.approved_at = timezone.now(); sig.approved_impact = sig.impact; sig.save()
        AdCampaign.objects.filter(store=self.store, name=sig.entity).delete()  # the fix: campaign stopped
        self._run()
        sig.refresh_from_db()
        self.assertEqual(sig.status, "resolved")
        snap = monitor.snapshot(self.store)
        self.assertEqual(snap["loop"]["fixed"], 1)
        self.assertGreater(snap["loop"]["fixed_impact"], 0)
        self.assertIn(sig.id, [w["id"] for w in snap["wins"]])

    def test_dismissed_signal_stays_dismissed(self):
        from core.models import Signal
        self._run()
        s = Signal.objects.first(); s.status = "dismissed"; s.save()
        self._run()
        s.refresh_from_db()
        self.assertEqual(s.status, "dismissed")


class AIFallbackTests(TestCase):
    def test_second_groq_model_is_used_when_first_is_rate_limited(self):
        from django.test import override_settings
        from core.ai import providers

        class R:
            def __init__(self, code, body): self.status_code, self._b, self.text, self.headers = code, body, "rate limit", {}
            def json(self): return self._b

        seen = []
        def fake_post(url, headers=None, json=None, timeout=None):
            seen.append(json["model"])
            if json["model"] == "big":
                return R(429, {})
            return R(200, {"choices": [{"message": {"content": "ok"}}]})

        with override_settings(GROQ_API_KEY="k", GROQ_MODEL="big", GROQ_FALLBACK_MODELS=["small"], AI_PROVIDERS=["groq", "gemini"]), \
             mock.patch("core.ai.providers.requests.post", fake_post), mock.patch("core.ai.providers.time.sleep"):
            text, provider = providers.complete([{"role": "user", "content": "hi"}])
        self.assertEqual((text, provider), ("ok", "groq"))
        self.assertEqual(seen, ["big", "small"])  # did not wait on the limited model, moved straight on
