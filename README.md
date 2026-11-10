# Kupwara Farmer Enrollment Portal (v40)

Sister portal to the Bandipora Farmer ID Progress dashboard, for Kupwara district (J&K).
Flask + Gunicorn, deployed on Render, auto-committing admin writes to GitHub.

## v40 model (simplified)

* **Master list of villages** comes straight from the Aug + Sep + Oct 2026 AGRISTACK CSVs —
  331 villages, 16 tehsils. Every CSV village appears on the dashboard regardless of its
  target.
* **Village name matching** is hard-norm (strip whitespace + punctuation), so "Lal Pora"
  and "Lalpora" always collapse to the same row — no duplication.
* **Monthly baselines** (`data/baselines/YYYY-MM.csv`) hold historical month totals.
* **Daily uploads** go to `data/YYYY-MM-DD.csv`. If the CSV mentions a village that is
  NOT in the master list, the upload is held in a pending state and the admin is asked
  to tehsil + target + pendency for each new village before the upload finalises.

## Layout

```
app.py                       Flask backend
default_targets.py           Master list (16 tehsils, 331 villages — from CSVs)
Procfile                     Gunicorn worker (1 worker, 120s timeout)
requirements.txt             Python deps
templates/                   Jinja2 pages
data/
  targets.json               Admin-set targets per village (seeded: 300 villages, 227,086)
  pendency.json              Admin-set pendency per village (seeded: 299 villages, 191,145)
  added_villages.json        Admin-added villages (not in master CSVs)
  deleted_villages.json      Admin-deleted village keys (recycle bin)
  baselines/YYYY-MM.csv      One per completed month (upload via admin)
  YYYY-MM-DD.csv             Daily snapshots within the current month
  pending/YYYY-MM-DD.csv     In-flight uploads waiting for new-village confirmation
```

## Deploy to Render (first time)

1. **GitHub repo** — create (e.g. `peerzadaobaid/Kupwara-Farmer-Portal`) and upload every file
   in this folder. Keep the `templates/` and `data/` sub-folders.
2. **Render → New → Web Service** → connect to that repo.
   * Build command: `pip install -r requirements.txt`
   * Start command: leave blank (Procfile handles it)
   * Instance type: Free
3. **Environment variables** on the Render service:
   * `ADMIN_USER` = `admin`
   * `ADMIN_PASS` = your admin password
   * `FLASK_SECRET_KEY` = any long random string
   * `GH_TOKEN` = GitHub PAT with `repo` scope
   * `GH_REPO` = `peerzadaobaid/Kupwara-Farmer-Portal`
   * `GH_BRANCH` = `main`
4. Deploy. First build ~3-4 min.
5. Open the Render URL, log in, and start uploading baselines / daily snapshots.

## Admin workflow

| Page | What it does |
|---|---|
| `/` | Public dashboard (tehsil-wise + village drill-down) |
| `/admin` | Admin index |
| `/admin/month-baselines` | Upload one CSV per completed month; close the current month |
| `/upload` | Daily CSV upload (within current month); new-village confirmation flow |
| `/admin/targets` | Set target + pendency per village. Add / delete villages. |
| `/analysis` | Top/bottom villages, at-risk list, tehsil ranking |
| `/download/xlsx` | Admin-only Excel download |

## New-village flow on daily upload

1. Pick date + CSV, submit.
2. If every village is already known → upload saves immediately.
3. If any village is new → yellow confirmation card appears. For each new village:
   tehsil dropdown (defaulted from the CSV), target input, pendency input.
4. Click **Confirm & finalize upload** → the CSV saves and the villages commit to
   `added_villages.json` in the same step.

No implicit additions — every new village enters with a tehsil + target that admin consciously
chose.
