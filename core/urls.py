from django.urls import path

from core import views

urlpatterns = [
    path("stores/", views.stores),
    path("stores/demo/", views.load_demo_store),
    path("stores/shopify/", views.connect_shopify),
    path("stores/<int:store_id>/", views.store_detail),
    path("stores/<int:store_id>/sync/", views.sync_store),
    path("stores/<int:store_id>/import-ads/", views.import_ads),
    path("ads-template/", views.ads_template),
    path("overview/", views.overview),
    path("products/", views.products),
    path("campaigns/", views.campaigns),
    path("findings/", views.findings),
    path("analysis/", views.analysis),
    path("chart-notes/", views.chart_notes),
    path("ads-report/", views.ads_report_view),
    path("ads-report/export/", views.ads_report_export),
    path("command/", views.command),
    path("signals/<int:signal_id>/", views.signal_detail),
    path("chat/", views.chat),
]
