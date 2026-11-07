# Kupwara Farmer Enrollment Portal

Sister portal to the Bandipora Farmer ID Progress dashboard, for Kupwara district (J&K).
Flask + Gunicorn, deployed on Render, auto-committing admin writes to GitHub.

## Layout

```
app.py                           Flask backend (all routes)
default_targets.py               Kupwara master list — 15 tehsils, 334 villages
Procfile                         Render process definition
requirements.txt                 Python deps
templates/                       Jinja2 templates (one per page)
data/
  baselines/YYYY-MM.csv          One CSV per completed month (uploaded via admin)
  YYYY-MM-DD.csv                 Daily snapshots within the current month
  targets.json                   Admin target overrides
  village_tehsil_overrides.json  Admin tehsil re-assignments
  unbucketed.json                Admin explicit-hide flags
  csv_village_aliases.json       CSV spelling → portal canonical mappings
```

## Deploy to Render (first time)

1. Create a new GitHub repo (e.g. `peerzadaobaid/Kupwara-Farmer-Portal`).
2. Upload every file in this folder to the repo root (keep the `templates/` folder structure).
3. On Render → New → Web Service → connect to that repo.
   - Build command: `pip install -r requirements.txt`
   - Start command: leave blank (Procfile handles it)
   - Instance type: Free
4. Set environment variables on the Render service:
   - `ADMIN_USER` = `admin` (or whatever you prefer)
   - `ADMIN_PASS` = your admin password
   - `FLASK_SECRET_KEY` = any long random string (session cookies)
   - `GH_TOKEN` = a GitHub PAT with `repo` scope
   - `GH_REPO` = `peerzadaobaid/Kupwara-Farmer-Portal`
   - `GH_BRANCH` = `main`
5. Deploy. First deploy takes ~3-4 min.
6. Open the Render URL. Log in via `/login`.

## How baselines and uploads work

- Historical months (Aug, Sep, Oct 2026) → upload each month-end CSV on the
  **Monthly Baselines** admin page. Each file is month-only enrollments. The
  dashboard baseline = sum of all uploaded months.
- Current month (Nov 2026 onwards) → upload daily CSVs via the **Upload daily**
  page. AGRISTACK CSVs are cumulative-within-month, so the latest daily snapshot
  for any given month represents the month-to-date total.
- At month-end → click **Close current month** on the Monthly Baselines page.
  The latest daily snapshot is promoted into `data/baselines/YYYY-MM.csv`, and
  daily uploads for the next month start fresh.

## Admin targets

- All 334 master-list villages are shown, grouped by tehsil (collapsible per tehsil).
- Each row has:
  - **Tehsil dropdown** — pick any of the 15 Kupwara tehsils to re-assign the village
    if AGRISTACK filed it under the wrong tehsil. Original master assignment is
    preserved; only the override is stored.
  - **Target** — integer. Set to 0 or blank to **unbucket** the village (hidden
    from dashboard until a target is set).
- CSV-unknown villages (seen in an uploaded CSV but not in the master list)
  appear in a separate section at the bottom, with a tehsil dropdown and target
  input so you can bring them into the dashboard.

## Dashboard columns

Tehsil-wise: Tehsil · Villages · Target · Total Issued · Issue % · Additions between · Approved · Approval %

Village-wise (per tehsil): same minus the villages count, village name replaces tehsil name.

## Not included (intentionally stripped from Bandipora)

- Camp Director tabs, admin pages, data model
- First Bucket badge/logic
- "Started 20 Oct" / new-batch admin controls
- Pending Entries Google Sheet integration

## Local testing (optional)

```bash
pip install -r requirements.txt
ADMIN_PASS=whatever python app.py
```

Open `http://localhost:5000`.
