from decimal import Decimal

from django.db import models


class Store(models.Model):
    """One connected shop. `platform` decides which connector feeds it."""

    PLATFORMS = [("demo", "Demo data"), ("shopify", "Shopify")]

    name = models.CharField(max_length=120)
    platform = models.CharField(max_length=20, choices=PLATFORMS, default="demo")
    domain = models.CharField(max_length=255, blank=True)  # e.g. my-shop.myshopify.com
    currency = models.CharField(max_length=3, default="CHF")

    # Shopify credentials. POC: stored as plain text in SQLite. Encrypt (e.g. django-fernet-fields
    # or a secrets manager) before this holds a real merchant's token.
    access_token = models.CharField(max_length=255, blank=True)
    client_id = models.CharField(max_length=255, blank=True)
    client_secret = models.CharField(max_length=255, blank=True)

    # Cost assumptions: things Shopify does not hand us per order. Editable in the UI.
    payment_fee_pct = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal("2.90"))
    payment_fee_fixed = models.DecimalField(max_digits=6, decimal_places=2, default=Decimal("0.30"))
    default_shipping_cost = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("7.00"))
    return_handling_cost = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("6.00"))

    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_sync_message = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.platform})"


class Product(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    external_id = models.CharField(max_length=100)
    title = models.CharField(max_length=300)
    sku = models.CharField(max_length=100, blank=True)
    category = models.CharField(max_length=100, blank=True)
    vendor = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = [("store", "external_id")]

    def __str__(self):
        return self.title


class Order(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="orders")
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(db_index=True)
    cancelled = models.BooleanField(default=False)
    source = models.CharField(max_length=40, blank=True, default="direct")  # google / meta / tiktok / email / organic / direct

    shipping_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    shipping_cost = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    payment_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    class Meta:
        unique_together = [("store", "external_id")]
        indexes = [models.Index(fields=["store", "created_at"])]


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="lines")
    title = models.CharField(max_length=300, blank=True)
    quantity = models.PositiveIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)  # before discounts
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # line total discount
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    refunded_quantity = models.PositiveIntegerField(default=0)


class AdCampaign(models.Model):
    PLATFORMS = [("google", "Google Ads"), ("meta", "Meta"), ("tiktok", "TikTok")]

    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="campaigns")
    platform = models.CharField(max_length=20, choices=PLATFORMS)
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=200)
    objective = models.CharField(max_length=60, blank=True)
    # Set for product-level campaigns (e.g. Google Shopping on one SKU) so spend lands on that product.
    product = models.ForeignKey(Product, on_delete=models.SET_NULL, null=True, blank=True, related_name="campaigns")

    class Meta:
        unique_together = [("store", "platform", "external_id")]


class AdDaily(models.Model):
    campaign = models.ForeignKey(AdCampaign, on_delete=models.CASCADE, related_name="daily")
    date = models.DateField(db_index=True)
    spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impressions = models.PositiveIntegerField(default=0)
    clicks = models.PositiveIntegerField(default=0)
    conversions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conversion_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    class Meta:
        unique_together = [("campaign", "date")]


class AnalysisReport(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="reports")
    kind = models.CharField(max_length=20, default="review")  # review | charts
    data_hash = models.CharField(max_length=40, blank=True)  # fingerprint of the numbers the text was written from
    period_days = models.PositiveIntegerField(default=30)
    provider = models.CharField(max_length=40, blank=True)
    content = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class MonitorRun(models.Model):
    """One pass of the monitor over a store's data."""
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="runs")
    created_at = models.DateTimeField(auto_now_add=True)
    period_days = models.PositiveIntegerField(default=30)
    trigger = models.CharField(max_length=20, default="manual")  # manual | schedule
    health = models.JSONField(default=dict)   # overall score + sub-scores
    brief = models.JSONField(default=dict)    # AI morning brief
    stats = models.JSONField(default=dict)    # new / worsened / resolved counts, money at stake
    provider = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-created_at"]


class Signal(models.Model):
    """A problem or opportunity the monitor found. Lives across runs: new -> approved -> resolved ("fixed")."""
    STATUS = [("new", "New"), ("approved", "Approved"), ("snoozed", "Snoozed"), ("dismissed", "Dismissed"), ("resolved", "Resolved")]
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="signals")
    fingerprint = models.CharField(max_length=300)  # kind:entity, stable across runs
    kind = models.CharField(max_length=40)
    kind_group = models.CharField(max_length=20, default="problem")  # problem | opportunity | risk
    severity = models.CharField(max_length=10, default="medium")
    title = models.CharField(max_length=300)
    entity = models.CharField(max_length=300, blank=True)
    detail = models.TextField(blank=True)
    impact = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    impact_prev = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    viz = models.JSONField(default=dict)
    ai = models.JSONField(default=dict)       # headline, why[], steps[], effort, confidence
    ai_hash = models.CharField(max_length=40, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default="new")
    active = models.BooleanField(default=True)  # still detected in the latest run
    first_run = models.ForeignKey(MonitorRun, null=True, on_delete=models.SET_NULL, related_name="born")
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    approved_impact = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        unique_together = [("store", "fingerprint")]
        ordering = ["-impact"]
