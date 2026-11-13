# Profit Desk: backend (Django REST API)

The API behind an AI profit dashboard for online stores. It connects Shopify (live) and ad platforms (CSV for now),
calculates true profit after every cost, runs a monitor that finds problems and opportunities, and answers questions
through an AI layer that only reasons over numbers the engine calculated.

Frontend: https://github.com/ajaxarrow/ai-profit-dashboard-frontend

Anwender-Anleitung (Deutsch, Schritt für Schritt durch die Oberfläche): https://github.com/ajaxarrow/ai-profit-dashboard-frontend/blob/main/docs/ANLEITUNG.md

## Architecture
```
Connectors (one file each)        Normalised tables             Engine                 AI layer
 Shopify (live, GraphQL)   ──┐    Product / Order / OrderLine   services/profit.py     ai/analyst.py
 Demo seed                 ──┼──► AdCampaign / AdDaily     ───► services/insights.py   ai/providers.py
 Ad-spend CSV import       ──┤    Signal / MonitorRun           services/ads_report.py  Groq (2 models) → Gemini
 Google Ads / Meta / TikTok / GA4 ─┘ (planned: implement `Connector`)  services/monitor.py
```
- **Profit** = sales after discounts − refunds − COGS − payment fees − net shipping − return handling − ad spend. Fee, shipping and return costs are editable assumptions per store.
- **The AI never calculates.** It explains numbers from the engine, so it cannot invent figures.
- **Monitor** (`services/monitor.py`): compares the last 7 days with the 7 before, finds loss-making products and campaigns, returns, discount leaks, scale-up chances and trends, keeps them as signals (new → approved → resolved) and counts approved problems that disappear as wins.
- **Advertising report**: per-platform and per-campaign efficiency, platform claims versus what the store recorded, spend-versus-sales correlation (raw and weekday-adjusted, with lag), Excel and CSV export.
- **AI providers**: Groq's large model, then a second Groq model (each model has its own rate limit), then Gemini.

## Run locally
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # add GROQ_API_KEY and/or GEMINI_API_KEY
.venv/bin/python manage.py migrate
.venv/bin/python manage.py loaddata demo     # the bundled snapshot (a Shopify dev store and a demo shop), or: seed_demo
.venv/bin/python manage.py runserver 8000
.venv/bin/python manage.py test
```

## Deploy on Render
`render.yaml` is a blueprint: New → Blueprint → pick this repo. Then set `GROQ_API_KEY` and `GEMINI_API_KEY` in the service's environment.

- **Data**: Render's disk is wiped whenever the service restarts, deploys or wakes from sleep, so the start command runs `migrate` then `loaddata demo` (`core/fixtures/demo.json.gz`, credentials removed). Anything changed on the live site is forgotten at the next restart.
- **`PROFIT_AS_OF`**: the data is a snapshot, so "today" is pinned to the date in this variable. Without it, the periods drift away from the data.
- **`READ_ONLY=1`**: blocks connecting, syncing, importing into and deleting stores. Chat, the monitor and approving signals still work.
- Refresh the snapshot: `python manage.py dump_demo_data` (writes the fixture, strips credentials).

## Environment
| Variable | Purpose |
|---|---|
| `GROQ_API_KEY`, `GEMINI_API_KEY` | AI providers. `GROQ_MODEL`, `GROQ_FALLBACK_MODELS`, `GEMINI_MODEL` are optional |
| `AI_PROVIDERS` | Order tried, default `groq,gemini` |
| `READ_ONLY`, `PROFIT_AS_OF` | Public-demo mode and the pinned date |
| `DJANGO_DEBUG`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` | Standard Django settings for production |
| `DATABASE_PATH` | SQLite file location (default `db.sqlite3`) |

## Connecting a real Shopify store (local)
Give a Shopify app `read_products read_orders read_inventory` (add `write_products write_orders write_inventory` to seed a dev store), approve protected customer data access (Analytics, Store management, no customer fields), install it, then use the frontend's Data sources page or `manage.py connect_shopify_env`. `manage.py seed_shopify` creates demo products and backdated orders in a dev store; `manage.py export_demo_ads --fit-store <id>` makes a sample ad-spend CSV sized to that store.

## Known limits
- No authentication; Shopify credentials are stored in plain text in SQLite. Add auth and encrypt secrets before using real merchant data.
- Approving a signal records and tracks the decision. It does not change prices or ad budgets yet.
- Google Ads, Meta, TikTok, Merchant Center and GA4 connectors are not built; ad data arrives by CSV.
- Payment fees, shipping and return handling come from assumptions, not from Shopify.
