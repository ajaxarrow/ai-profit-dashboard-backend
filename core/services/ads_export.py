"""Export the advertising report as an Excel workbook or CSV."""
import csv
import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

CAMPAIGN_COLS = [
    ("Platform", "platform"), ("Campaign", "name"), ("Objective", "objective"), ("Spend", "spend"), ("Share of spend %", "share_of_spend_pct"),
    ("Impressions", "impressions"), ("Clicks", "clicks"), ("CTR %", "ctr_pct"), ("CPC", "cpc"), ("Conversions", "conversions"), ("CPA", "cpa"),
    ("Reported sales", "conversion_value"), ("ROAS (reported)", "roas"), ("Break-even ROAS", "break_even_roas"),
    ("Profit after ads", "profit_after_ads"), ("ROAS 1st half", "roas_first_half"), ("ROAS 2nd half", "roas_second_half"),
    ("Spend vs ROAS correlation", "spend_vs_roas_r"), ("Verdict", "verdict"),
]
PLATFORM_COLS = [
    ("Platform", "name"), ("Spend", "spend"), ("Share of spend %", "share_of_spend_pct"), ("Impressions", "impressions"), ("Clicks", "clicks"),
    ("CTR %", "ctr_pct"), ("CPC", "cpc"), ("Conversions", "conversions"), ("CPA", "cpa"), ("Reported sales", "reported_sales"), ("ROAS (reported)", "roas"),
    ("Profit after ads", "profit_after_ads"), ("Orders the store recorded", "store_orders"), ("Revenue the store recorded", "store_revenue"),
    ("Platform-claimed sales / store revenue", "claim_vs_store"), ("Platform conversions / store orders", "conversions_vs_orders"),
]
CORR_COLS = [
    ("Relationship", "label"), ("Days compared", "n"), ("Best lag (days spend leads)", "lag_days"), ("Correlation r (raw)", "r"),
    ("Correlation r (weekday-adjusted)", "r_adjusted"), ("Strength (adjusted)", "strength"), ("Direction", "direction"),
    ("Slope (outcome per 1 spend)", "slope"), ("R squared (raw)", "r2"),
]


def _daily_cols(report):
    plats = [k[6:] for k in report["daily"][0] if k.startswith("spend_")] if report["daily"] else []
    cols = [("Date", "date"), ("Ad spend", "spend")] + [(f"Spend {p}", f"spend_{p}") for p in plats] + [
        ("Platform-reported sales", "reported_sales"), ("Store revenue", "revenue"), ("Orders", "orders"),
        ("Avg order value", "aov"), ("Profit before ads", "profit_before_ads"), ("Profit after ads", "profit"), ("Revenue per ad franc", "mer")]
    return cols


def _rows(items, cols):
    return [[it.get(k) for _, k in cols] for it in items]


def to_csv(report: dict, which: str) -> str:
    spec = {"campaigns": (report["campaigns"], CAMPAIGN_COLS), "platforms": (report["platforms"], PLATFORM_COLS),
            "daily": (report["daily"], _daily_cols(report)), "correlations": (report["correlations"], CORR_COLS)}
    items, cols = spec[which]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([h for h, _ in cols])
    w.writerows(_rows(items, cols))
    return buf.getvalue()


def to_xlsx(report: dict, store_name: str, notes: dict | None = None) -> bytes:
    wb = Workbook()
    head_fill, head_font = PatternFill("solid", fgColor="EFE8CB"), Font(bold=True, color="1D1C18")

    def sheet(ws, cols, items):
        ws.append([h for h, _ in cols])
        for c in ws[1]:
            c.fill, c.font, c.alignment = head_fill, head_font, Alignment(wrap_text=True, vertical="top")
        for row in _rows(items, cols):
            ws.append(row)
        for i, (h, _) in enumerate(cols, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in ws.iter_rows(min_row=2, values_only=True) if r[i - 1] is not None][:200]) if ws.max_row > 1 else len(h)
            ws.column_dimensions[get_column_letter(i)].width = min(max(12, width + 2), 42)
        ws.freeze_panes = "A2"

    ws = wb.active
    ws.title = "Summary"
    k, cur, per = report["kpis"], report["currency"], report["period"]
    ws.append([f"Advertising report: {store_name}"]); ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Period: {per['start']} to {per['end']} ({per['days']} days). Currency: {cur}"])
    ws.append([])
    for label, key in [("Ad spend", "spend"), ("Previous period spend", "spend_prev"), ("Platform-reported sales", "reported_sales"),
                       ("ROAS (reported)", "roas"), ("Break-even ROAS", "break_even_roas"), ("Revenue per ad franc (MER)", "mer"),
                       ("Ad spend as % of revenue", "spend_share_of_revenue_pct"), ("Profit after ads (platform-reported sales x margin)", "profit_after_ads_reported"),
                       ("Store profit (all costs)", "store_profit"), ("Spend on losing campaigns", "losing_spend"), ("Money lost by those campaigns", "losing_amount"),
                       ("Clicks", "clicks"), ("Impressions", "impressions"), ("Conversions", "conversions")]:
        ws.append([label, k.get(key)])
    ws.append([])
    if notes:
        ws.append(["AI commentary"]); ws.cell(ws.max_row, 1).font = Font(bold=True)
        for key, text in notes.items():
            ws.append([key.replace("_", " ").capitalize(), text])
    ws.append([])
    ws.append(["Note: correlation shows association, not proof that spend caused sales. 'Weekday-adjusted' removes the Monday-to-Sunday rhythm shared by spend and orders."])
    ws.column_dimensions["A"].width = 46; ws.column_dimensions["B"].width = 100
    for r in ws.iter_rows(min_row=4):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")

    sheet(wb.create_sheet("Platforms"), PLATFORM_COLS, report["platforms"])
    sheet(wb.create_sheet("Campaigns"), CAMPAIGN_COLS, report["campaigns"])
    sheet(wb.create_sheet("Daily"), _daily_cols(report), report["daily"])
    sheet(wb.create_sheet("Weekly"), [("Week from", "from"), ("Spend", "spend"), ("Revenue", "revenue"), ("Orders", "orders"), ("Profit", "profit"), ("Revenue per ad franc", "mer")], report["weekly"])
    sheet(wb.create_sheet("Correlations"), CORR_COLS, report["correlations"])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
