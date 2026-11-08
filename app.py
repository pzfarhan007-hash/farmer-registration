"""
Kupwara Farmer Enrollment Portal
================================
Flask app for Render deployment. Sister portal to the Bandipora Farmer ID
Progress dashboard, without the Camp Director / First Bucket / Pending
Entries / New Batch concepts.

Data:
  - data/baselines/YYYY-MM.csv    one per completed month (month-only AGRISTACK)
  - data/YYYY-MM-DD.csv           daily snapshots for the current month

Admin pages:
  - /login, /logout, /admin
  - /upload              daily snapshot upload (current month only)
  - /admin/month-baselines   upload one CSV per completed month + close month
  - /admin/targets       tehsil-wise village list; target=0 hides from dashboard
  - /admin/csv-reconciliation  map AGRISTACK village spellings to portal names

Every village has a tehsil dropdown in the admin targets page so wrong
tehsil assignments can be corrected on the fly.
"""
import csv
import glob
import json
import os
import re
import secrets as _secrets
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, Response, abort, render_template, request, jsonify, session, redirect, url_for, flash

# =========================================================================
# BAKED-IN DATA — targets, tehsils (master list for Kupwara district)
# Everything else (monthly baselines, overrides, aliases) lives on disk.
# =========================================================================

PORTAL_TITLE = "Kupwara District — Farmer Enrollment Dashboard"
PORTAL_SHORT = "Kupwara Farmer ID Progress"
DISTRICT_NAME = "Kupwara"

# Default admin login — change in code or via env (ADMIN_USER, ADMIN_PASS).
DEFAULT_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASS", "Password.1719@")

from default_targets import KUPWARA_TEHSILS, DEFAULT_TARGETS

# Safe fallback when NO monthly baselines exist yet.
_BASELINE_FALLBACK_DATE  = "2026-07-31"
_BASELINE_FALLBACK_LABEL = "no baseline uploaded yet"

# =========================================================================
# Paths
# =========================================================================

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
MONTH_BASELINES_DIR = os.path.join(DATA_DIR, "baselines")
TARGETS_OVERRIDE_FILE = os.path.join(DATA_DIR, "targets.json")
VILLAGE_TEHSIL_OVERRIDE_FILE = os.path.join(DATA_DIR, "village_tehsil_overrides.json")
UNBUCKETED_FILE = os.path.join(DATA_DIR, "unbucketed.json")   # explicit "hide" flags (rare — target=0 usually enough)
CSV_ALIAS_FILE = os.path.join(DATA_DIR, "csv_village_aliases.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "portal_settings.json")

# =========================================================================
# Flask
# =========================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", _secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# =========================================================================
# Normalization / alias helpers
# =========================================================================

def norm_name(s):
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

# Canonical tehsil names (UPPERCASE-normalized) built from Kupwara master list.
_TEHSIL_CANONICAL = {norm_name(t): t for t in KUPWARA_TEHSILS}

def canonical_tehsil(t):
    """Map any tehsil spelling to the canonical display name from the master list."""
    n = norm_name(t)
    return _TEHSIL_CANONICAL.get(n, t)

# v37.2 — village-name-primary keying.
# AGRISTACK CSV's tehsil assignment is authoritative per the user. 329 of 334
# master villages have unique names, so for them we key by village name only —
# whatever tehsil the CSV reports is the one the dashboard displays. For the
# remaining 5 village names that appear in multiple tehsils (Batapora, Dard
# Pora, Kawari, Push Wari, Tulwari), both tehsil AND village must still match
# to disambiguate.
_DUPLICATE_VILLAGE_NORMS = set()
def _init_duplicate_village_norms():
    counts = {}
    for t, v, _ in DEFAULT_TARGETS:
        n = norm_name(v)
        counts[n] = counts.get(n, 0) + 1
    _DUPLICATE_VILLAGE_NORMS.update(n for n, c in counts.items() if c > 1)
_init_duplicate_village_norms()

def norm_key(tehsil, village):
    n_vil = _apply_village_alias(norm_name(village))
    if n_vil in _DUPLICATE_VILLAGE_NORMS:
        return canonical_tehsil(tehsil).upper() + "||" + n_vil
    return "||" + n_vil   # empty tehsil prefix = unique name, any tehsil matches

def _apply_village_alias(norm_village):
    """Map a normalized village name to its portal-canonical form via CSV
    alias overrides. Returns input unchanged if no alias."""
    aliases = _load_csv_village_aliases()
    return aliases.get(norm_village, norm_village)

# =========================================================================
# JSON overrides on disk (committed to GitHub when GH_TOKEN present)
# =========================================================================

def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)

def load_targets_overrides():
    return _read_json(TARGETS_OVERRIDE_FILE, {})

def load_village_tehsil_overrides():
    """{norm_key: canonical_tehsil_name} — correct AGRISTACK-misassigned tehsils."""
    return _read_json(VILLAGE_TEHSIL_OVERRIDE_FILE, {})

def load_unbucketed_flags():
    """{norm_key: true} — explicit hide even if target > 0 (rare)."""
    return _read_json(UNBUCKETED_FILE, {})

def load_settings():
    return _read_json(SETTINGS_FILE, {})

_csv_alias_cache = {"loaded": False, "data": {}, "mtime": 0}
def _load_csv_village_aliases():
    """Cache the alias map with mtime invalidation so we don't stat on every
    norm_key call."""
    try:
        mt = os.path.getmtime(CSV_ALIAS_FILE)
    except OSError:
        if _csv_alias_cache["loaded"]:
            return _csv_alias_cache["data"]
        _csv_alias_cache.update({"loaded": True, "data": {}, "mtime": 0})
        return {}
    if _csv_alias_cache["loaded"] and _csv_alias_cache["mtime"] == mt:
        return _csv_alias_cache["data"]
    d = _read_json(CSV_ALIAS_FILE, {})
    _csv_alias_cache.update({"loaded": True, "data": d, "mtime": mt})
    return d

def _invalidate_csv_alias_cache():
    _csv_alias_cache["loaded"] = False

# =========================================================================
# Parse one AGRISTACK snapshot CSV
# =========================================================================

TEHSIL_HEADERS  = ["subdistrictname", "sub_district_name", "subdistrict", "tehsil"]
VILLAGE_HEADERS = ["villagename", "village_name", "village"]
STATUS_HEADERS  = ["approvalstatus", "approval_status", "status"]

def find_header_idx(header, options):
    norm = [re.sub(r"[^a-z0-9]", "", (h or "").lower()) for h in header]
    for opt in options:
        key = re.sub(r"[^a-z0-9]", "", opt.lower())
        if key in norm:
            return norm.index(key)
    return -1

def normalize_status(s):
    return (s or "").strip().upper()

_snapshot_parse_cache = {}   # path -> (mtime, counts)

def parse_date_from_filename(path):
    name = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$|^(\d{2})[.\-](\d{2})[.\-](\d{4})$", name)
    if not m:
        return None
    g = m.groups()
    if g[0]:
        return "{}-{}-{}".format(g[0], g[1], g[2])
    return "{}-{}-{}".format(g[5], g[4], g[3])

def parse_snapshot_csv(path):
    """Parse one CSV -> {village_key: {issued, approved, tehsil, village}} aggregated."""
    counts = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return counts
        t_idx = find_header_idx(header, TEHSIL_HEADERS)
        v_idx = find_header_idx(header, VILLAGE_HEADERS)
        s_idx = find_header_idx(header, STATUS_HEADERS)
        if t_idx < 0 or v_idx < 0 or s_idx < 0:
            raise ValueError("{}: header must have tehsil, village and status columns".format(os.path.basename(path)))
        for row in reader:
            if not row or len(row) <= max(t_idx, v_idx, s_idx):
                continue
            tehsil = (row[t_idx] or "").strip()
            village = (row[v_idx] or "").strip()
            if not tehsil or not village:
                continue
            key = norm_key(tehsil, village)
            rec = counts.setdefault(key, {
                "tehsil": tehsil, "village": village,
                "issued": 0, "approved": 0,
            })
            rec["issued"] += 1
            if normalize_status(row[s_idx]) == "APPROVED":
                rec["approved"] += 1
    return counts

# =========================================================================
# Monthly baselines
# =========================================================================

_MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.csv$")

def _month_end_date(ym):
    y, m = ym.split("-"); y = int(y); m = int(m)
    nxt = datetime(y+1, 1, 1) if m == 12 else datetime(y, m+1, 1)
    return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")

def _month_label(ym):
    try:
        return datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    except Exception:
        return ym

def list_month_baseline_files():
    out = []
    if not os.path.isdir(MONTH_BASELINES_DIR):
        return out
    for path in sorted(glob.glob(os.path.join(MONTH_BASELINES_DIR, "*.csv"))):
        m = _MONTH_FILE_RE.match(os.path.basename(path))
        if m:
            out.append(("{}-{}".format(m.group(1), m.group(2)), path))
    out.sort(key=lambda x: x[0])
    return out

def load_month_baselines():
    sum_counts = {}
    months_meta = []
    for ym, path in list_month_baseline_files():
        try:
            mt = os.path.getmtime(path)
            cached = _snapshot_parse_cache.get(path)
            if cached and cached[0] == mt:
                snap = cached[1]
            else:
                snap = parse_snapshot_csv(path)
                _snapshot_parse_cache[path] = (mt, snap)
        except Exception as e:
            months_meta.append({"month": ym, "label": _month_label(ym), "end": _month_end_date(ym),
                                "issued": 0, "approved": 0, "villages": 0, "path": path, "error": str(e)})
            continue
        m_iss = m_app = 0
        for k, v in snap.items():
            prev = sum_counts.get(k, {})
            sum_counts[k] = {
                "tehsil":   prev.get("tehsil", v["tehsil"]),
                "village":  prev.get("village", v["village"]),
                "issued":   prev.get("issued", 0)   + v["issued"],
                "approved": prev.get("approved", 0) + v["approved"],
            }
            m_iss += v["issued"]; m_app += v["approved"]
        months_meta.append({"month": ym, "label": _month_label(ym), "end": _month_end_date(ym),
                            "issued": m_iss, "approved": m_app, "villages": len(snap), "path": path})
    return sum_counts, months_meta

def effective_baseline_date():
    files = list_month_baseline_files()
    if not files:
        return _BASELINE_FALLBACK_DATE
    return _month_end_date(files[-1][0])

def effective_baseline_label():
    files = list_month_baseline_files()
    if not files:
        return _BASELINE_FALLBACK_LABEL
    return "through " + datetime.strptime(effective_baseline_date(), "%Y-%m-%d").strftime("%d %b %Y")

def baseline_counts():
    sum_counts, _ = load_month_baselines()
    return {k: {"tehsil": v["tehsil"], "village": v["village"],
                "issued": v["issued"], "approved": v["approved"]}
            for k, v in sum_counts.items()}

# =========================================================================
# Discover daily snapshots + build cumulative state
# =========================================================================

def discover_snapshots():
    snaps = []
    if not os.path.isdir(DATA_DIR):
        return snaps
    cutoff = effective_baseline_date()
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        d = parse_date_from_filename(path)
        if d and d > cutoff:
            snaps.append((d, path))
    snaps.sort(key=lambda x: x[0])
    return snaps

def _merge(dst, src):
    """Max-merge within current-month daily snapshots (they grow monotonically)."""
    for k, v in src.items():
        prev = dst.get(k, {})
        dst[k] = {
            "tehsil":   prev.get("tehsil", v["tehsil"]),
            "village":  prev.get("village", v["village"]),
            "issued":   max(v["issued"],   prev.get("issued", 0)),
            "approved": max(v["approved"], prev.get("approved", 0)),
        }

def build_state():
    """Baseline + current-month running max, summed per village. See v37.1 notes."""
    base = baseline_counts()
    baseline_date = effective_baseline_date()
    per_date = {baseline_date: {k: dict(v) for k, v in base.items()}}
    sorted_dates = [baseline_date]
    errors = []
    current_month_max = {}
    for date, path in discover_snapshots():
        try:
            mt = os.path.getmtime(path)
            cached = _snapshot_parse_cache.get(path)
            if cached and cached[0] == mt:
                snap = cached[1]
            else:
                snap = parse_snapshot_csv(path)
                _snapshot_parse_cache[path] = (mt, snap)
        except Exception as e:
            errors.append(str(e)); continue
        _merge(current_month_max, snap)
        day_cum = {k: dict(v) for k, v in base.items()}
        for k, v in current_month_max.items():
            prev = day_cum.get(k, {})
            day_cum[k] = {
                "tehsil":   prev.get("tehsil", v["tehsil"]),
                "village":  prev.get("village", v["village"]),
                "issued":   prev.get("issued", 0)   + v["issued"],
                "approved": prev.get("approved", 0) + v["approved"],
            }
        per_date[date] = day_cum
        sorted_dates.append(date)
    cumulative = per_date[sorted_dates[-1]] if sorted_dates else {k: dict(v) for k, v in base.items()}
    return cumulative, per_date, sorted_dates, errors

def per_day_additions(per_date, sorted_dates):
    out = {}
    for i, d in enumerate(sorted_dates):
        current = per_date[d]
        if i == 0:
            tot = sum(v["issued"] for v in current.values())
            by_v = {k: v["issued"] for k, v in current.items()}
            out[d] = {"total": tot, "by_village": by_v}; continue
        prev = per_date[sorted_dates[i-1]]
        tot = 0; by_v = {}
        for k in set(current.keys()) | set(prev.keys()):
            c = current.get(k, {}).get("issued", 0)
            p = prev.get(k, {}).get("issued", 0)
            delta = c - p
            if delta == 0: continue
            by_v[k] = delta; tot += delta
        out[d] = {"total": tot, "by_village": by_v}
    return out

# =========================================================================
# Effective targets (default + overrides)
# =========================================================================

def effective_targets():
    """Yield (tehsil, village, target) for every known village (master + overrides)."""
    overrides = load_targets_overrides()
    seen = set()
    for t, v, tg in DEFAULT_TARGETS:
        k = norm_key(t, v)
        seen.add(k)
        cur = overrides.get(k, tg)
        try: cur = int(cur)
        except Exception: cur = tg
        yield (t, v, cur)
    # Admin-added villages (keys only in overrides)
    for k, cur in overrides.items():
        if k in seen: continue
        parts = k.split("||", 1)
        t = parts[0].title() if len(parts) == 2 else k
        v = parts[1].title() if len(parts) == 2 else ""
        try: cur = int(cur)
        except Exception: cur = 0
        yield (t, v, cur)

def villages_seen_in_uploads():
    """{norm_key: {tehsil, village}} for any village encountered in a baseline
    or a daily snapshot. Drives the 'unknown village' section on the targets
    page. First-seen display name wins."""
    seen = {}
    for _, path in list_month_baseline_files() + discover_snapshots():
        try:
            snap = parse_snapshot_csv(path)
        except Exception:
            continue
        for k, v in snap.items():
            if k not in seen:
                seen[k] = {"tehsil": v["tehsil"], "village": v["village"]}
    return seen

# =========================================================================
# Nested view shape (for dashboard.html)
# =========================================================================

_dashboard_cache = {"ts": 0, "data": None}
_DASHBOARD_TTL = 30

def _invalidate_dashboard_cache():
    _dashboard_cache["ts"] = 0
    _dashboard_cache["data"] = None

def build_nested_structure():
    now = datetime.utcnow().timestamp()
    if _dashboard_cache["data"] and (now - _dashboard_cache["ts"]) < _DASHBOARD_TTL:
        return _dashboard_cache["data"]

    cumulative, per_date, sorted_dates, errors = build_state()

    # Effective tehsil per village (overrides let admin correct an AGRISTACK miscategorization)
    tehsil_overrides = load_village_tehsil_overrides()
    unbucketed_flags = load_unbucketed_flags()

    # Build target map by norm_key (with overrides applied)
    targets = {}
    display = {}
    for t, v, tg in effective_targets():
        k = norm_key(t, v)
        targets[k] = tg
        display[k] = (canonical_tehsil(t), v)

    baseline_by_key = baseline_counts()
    def _bl(key):
        rec = baseline_by_key.get(key)
        return (rec["issued"], rec["approved"]) if rec else (0, 0)

    # v37.2 — tehsil display priority:
    #   1. Admin manual override (admin used the targets-page dropdown)
    #   2. AGRISTACK CSV tehsil (if the village has CSV data and the CSV tehsil
    #      is a valid Kupwara tehsil)
    #   3. Master-list tehsil (xlsx), as a last resort for villages with no
    #      CSV data yet
    def _effective_tehsil(key, cumrec, master_disp_t):
        if key in tehsil_overrides:
            return tehsil_overrides[key]
        if cumrec:
            csv_t = canonical_tehsil(cumrec.get("tehsil", ""))
            if csv_t in KUPWARA_TEHSILS:
                return csv_t
        return master_disp_t

    # All villages: master targets ∪ cumulative-seen
    villages = {}
    for key, tgt in targets.items():
        rec = cumulative.get(key)
        iss_b, app_b = _bl(key)
        master_t, disp_v = display.get(key, ("", ""))
        disp_t = _effective_tehsil(key, rec, master_t)
        if rec:
            villages[key] = {
                "tehsil": disp_t, "village": disp_v, "target": tgt,
                "issued": rec["issued"], "approved": rec["approved"],
                "issuedBaseline": iss_b, "approvedBaseline": app_b,
            }
        else:
            villages[key] = {
                "tehsil": disp_t, "village": disp_v, "target": tgt,
                "issued": iss_b, "approved": app_b,
                "issuedBaseline": iss_b, "approvedBaseline": app_b,
            }

    for key, rec in cumulative.items():
        if key in villages: continue
        iss_b, app_b = _bl(key)
        disp_t = _effective_tehsil(key, rec, rec["tehsil"])
        villages[key] = {
            "tehsil": disp_t, "village": rec["village"], "target": 0,
            "issued": rec["issued"], "approved": rec["approved"],
            "issuedBaseline": iss_b, "approvedBaseline": app_b,
        }

    # HIDE unbucketed (target == 0 OR explicit flag) from the dashboard entirely
    villages = {k: v for k, v in villages.items()
                if not unbucketed_flags.get(k)
                and (v.get("target", 0) or 0) > 0}

    # Attach per-village datedCounts (positive daily deltas)
    daily = per_day_additions(per_date, sorted_dates)
    for d, data in daily.items():
        for vk, delta in data["by_village"].items():
            if vk in villages and delta > 0:
                villages[vk].setdefault("datedCounts", {})
                villages[vk]["datedCounts"][d] = {"issued": delta, "approved": 0}
    # Approvals per date
    for i, d in enumerate(sorted_dates):
        current = per_date[d]
        for key, rec in current.items():
            if key not in villages: continue
            if i == 0:
                a_delta = rec["approved"]
            else:
                prev = per_date[sorted_dates[i-1]]
                a_delta = rec["approved"] - prev.get(key, {}).get("approved", 0)
            if a_delta > 0:
                villages[key].setdefault("datedCounts", {})
                villages[key]["datedCounts"].setdefault(d, {"issued": 0, "approved": 0})
                villages[key]["datedCounts"][d]["approved"] = a_delta
    for v in villages.values():
        v.setdefault("datedCounts", {})

    # Group by canonical tehsil
    tehsils = {t: {"tehsil": t, "target": 0, "issued": 0, "approved": 0,
                   "issuedBaseline": 0, "approvedBaseline": 0,
                   "villages": []} for t in KUPWARA_TEHSILS}
    for key, v in villages.items():
        t = canonical_tehsil(v["tehsil"])
        if t not in tehsils:
            tehsils[t] = {"tehsil": t, "target": 0, "issued": 0, "approved": 0,
                          "issuedBaseline": 0, "approvedBaseline": 0, "villages": []}
        obj = tehsils[t]
        obj["target"]           += v["target"]
        obj["issued"]           += v["issued"]
        obj["approved"]         += v["approved"]
        obj["issuedBaseline"]   += v["issuedBaseline"]
        obj["approvedBaseline"] += v["approvedBaseline"]
        obj["villages"].append(v)

    # Sort villages within each tehsil by issue % desc
    for t in tehsils.values():
        t["villages"].sort(key=lambda v: -(v["issued"]/v["target"]) if v["target"] > 0 else -2)
    # Drop tehsils with zero villages visible (optional — keeps dashboard tidy)
    tehsil_list = [t for t in tehsils.values() if t["villages"]]
    tehsil_list.sort(key=lambda t: -t["issued"])

    _baseline_date = effective_baseline_date()
    _baseline_label = effective_baseline_label()
    recent_dates = [d for d in sorted_dates if d > _baseline_date][-5:]
    last5 = [{"date": d, "total": daily[d]["total"]} for d in recent_dates]

    result = {
        "tehsils": tehsil_list,
        "last5": last5,
        "minDate": sorted_dates[0] if sorted_dates else _baseline_date,
        "maxDate": sorted_dates[-1] if sorted_dates else _baseline_date,
        "baselineDate": _baseline_date,
        "baselineLabel": _baseline_label,
        "generated": datetime.utcnow().isoformat() + "Z",
        "snapshotDates": [d for d in sorted_dates if d > _baseline_date],
        "errors": errors,
        "portalTitle": PORTAL_TITLE,
        "districtName": DISTRICT_NAME,
    }
    _dashboard_cache.update({"ts": now, "data": result})
    return result

# =========================================================================
# Login / admin session
# =========================================================================

def _is_admin():
    return bool(session.get("admin"))

@app.context_processor
def _ctx():
    return {
        "is_admin":    _is_admin(),
        "portal_title": PORTAL_TITLE,
        "portal_short": PORTAL_SHORT,
    }

def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not _is_admin():
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = (request.form.get("username") or "").strip()
        p = request.form.get("password") or ""
        if u == DEFAULT_ADMIN_USER and p == DEFAULT_ADMIN_PASS:
            session["admin"] = True
            return redirect(request.args.get("next") or url_for("admin"))
        error = "Invalid credentials."
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("dashboard"))

@app.route("/admin")
@admin_required
def admin():
    return render_template("admin.html",
                           baseline_label=effective_baseline_label(),
                           baseline_date=effective_baseline_date(),
                           tehsils=KUPWARA_TEHSILS,
                           n_villages=len(DEFAULT_TARGETS))

# =========================================================================
# Dashboard + health
# =========================================================================

@app.route("/")
def dashboard():
    return render_template("dashboard.html", state=build_nested_structure())

@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": datetime.utcnow().isoformat() + "Z"})

@app.template_filter("short_date")
def short_date_filter(ymd):
    if not ymd or len(ymd) < 10: return ymd or ""
    try:
        return datetime.strptime(ymd[:10], "%Y-%m-%d").strftime("%d %b '%y")
    except Exception:
        return ymd

# =========================================================================
# Upload daily snapshot
# =========================================================================

def _parse_date_input(raw):
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m: return raw
    m = re.match(r"^(\d{2})[./-](\d{2})[./-](\d{4})$", raw)
    if m: return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    return None

@app.route("/upload", methods=["GET", "POST"])
@admin_required
def upload():
    error = None; saved = None; replaced_date = None
    baseline_date = effective_baseline_date()
    if request.method == "POST":
        date = _parse_date_input(request.form.get("date", ""))
        if not date or date <= baseline_date:
            error = ("Date must be in dd-mm-yyyy and after {} (the latest closed-month "
                     "baseline). Pre-baseline months go through the Monthly Baselines "
                     "page.").format(baseline_date)
        else:
            out_path = os.path.join(DATA_DIR, date + ".csv")
            already_exists = os.path.exists(out_path)
            if already_exists and request.form.get("confirm_overwrite") != "yes":
                error = ("A snapshot for {} already exists. Tick 'Yes, replace it' and "
                         "resubmit to confirm the overwrite.").format(date)
            else:
                f = request.files.get("csv")
                if not f or not f.filename:
                    error = "Pick a CSV file."
                else:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    f.save(out_path)
                    try: parse_snapshot_csv(out_path)
                    except Exception as e:
                        os.remove(out_path); error = "That CSV didn't parse: {}".format(e)
                    else:
                        saved = date + ".csv"
                        if already_exists: replaced_date = date
                        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
                            try:
                                verb = "replace" if already_exists else "add"
                                commit_to_github(out_path, "data/" + date + ".csv",
                                                 "data: {} snapshot for {}".format(verb, date))
                                saved += " (committed to GitHub)"
                            except Exception as e:
                                error = "Saved locally but GitHub push failed: {}".format(e)
                        try: _snapshot_parse_cache.pop(out_path, None)
                        except Exception: pass
                        _invalidate_dashboard_cache()
    existing = [d for d, _ in discover_snapshots()]
    return render_template("upload.html",
                           baseline_date=baseline_date,
                           baseline_label=effective_baseline_label(),
                           existing_dates=existing,
                           replaced_date=replaced_date,
                           error=error, saved=saved)

# =========================================================================
# Monthly Baselines admin
# =========================================================================

def _slots_to_show():
    base_start = datetime(2026, 8, 1)
    now = datetime.utcnow()
    end = datetime(now.year, now.month, 1)
    files = list_month_baseline_files()
    if files:
        latest_ym = files[-1][0]
        y, m = latest_ym.split("-"); y = int(y); m = int(m)
        tail = datetime(y+1, 1, 1) if m == 12 else datetime(y, m+1, 1)
        if tail > end: end = tail
    slots = []
    cur = base_start
    while cur < end:
        slots.append(cur.strftime("%Y-%m"))
        cur = datetime(cur.year+1, 1, 1) if cur.month == 12 else datetime(cur.year, cur.month+1, 1)
    return slots

@app.route("/admin/month-baselines", methods=["GET", "POST"])
@admin_required
def admin_month_baselines():
    error = None; saved = None
    os.makedirs(MONTH_BASELINES_DIR, exist_ok=True)

    if request.method == "POST":
        action = request.form.get("action", "")
        if action == "upload":
            ym = (request.form.get("month") or "").strip()
            if not re.match(r"^\d{4}-\d{2}$", ym):
                error = "Pick a month."
            else:
                f = request.files.get("csv")
                if not f or not f.filename:
                    error = "Pick a CSV file for {}.".format(ym)
                else:
                    out_path = os.path.join(MONTH_BASELINES_DIR, ym + ".csv")
                    f.save(out_path)
                    try: parse_snapshot_csv(out_path)
                    except Exception as e:
                        os.remove(out_path); error = "That CSV didn't parse: {}".format(e)
                    else:
                        saved = "Saved {} baseline.".format(_month_label(ym))
                        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
                            try:
                                commit_to_github(out_path, "data/baselines/" + ym + ".csv",
                                                 "data: set month baseline for {}".format(ym))
                                saved += " Committed to GitHub."
                            except Exception as e:
                                error = "Saved locally but GitHub push failed: {}".format(e)
                        try: _snapshot_parse_cache.pop(out_path, None)
                        except Exception: pass
                        _invalidate_dashboard_cache()

        elif action == "delete":
            ym = (request.form.get("month") or "").strip()
            if not re.match(r"^\d{4}-\d{2}$", ym):
                error = "Bad month."
            else:
                out_path = os.path.join(MONTH_BASELINES_DIR, ym + ".csv")
                if os.path.exists(out_path):
                    try: os.remove(out_path)
                    except OSError as e: error = "Could not delete: {}".format(e)
                if not error:
                    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
                        try:
                            tmp = out_path + ".empty"
                            with open(tmp, "w", encoding="utf-8") as tf:
                                tf.write("districtName,subDistrictName,villageName,approvalStatus\n")
                            commit_to_github(tmp, "data/baselines/" + ym + ".csv",
                                             "data: clear month baseline for {}".format(ym))
                            os.remove(tmp)
                        except Exception as e:
                            error = "Deleted locally but GitHub clear failed: {}".format(e)
                    saved = "Cleared {} baseline.".format(_month_label(ym))
                    try: _snapshot_parse_cache.pop(out_path, None)
                    except Exception: pass
                    _invalidate_dashboard_cache()

        elif action == "close_month":
            snaps = discover_snapshots()
            if not snaps:
                error = "No daily snapshots to close. Upload at least one daily CSV first."
            else:
                latest_date, latest_path = snaps[-1]
                ym = latest_date[:7]
                out_path = os.path.join(MONTH_BASELINES_DIR, ym + ".csv")
                try:
                    with open(latest_path, "rb") as src, open(out_path, "wb") as dst:
                        dst.write(src.read())
                    parse_snapshot_csv(out_path)
                except Exception as e:
                    try: os.remove(out_path)
                    except OSError: pass
                    error = "Could not promote snapshot: {}".format(e)
                else:
                    saved = "Closed {} using snapshot {}.".format(_month_label(ym), latest_date)
                    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
                        try:
                            commit_to_github(out_path, "data/baselines/" + ym + ".csv",
                                             "data: close month {} (from {})".format(ym, latest_date))
                            saved += " Committed to GitHub."
                        except Exception as e:
                            error = "Saved locally but GitHub push failed: {}".format(e)
                    try: _snapshot_parse_cache.pop(out_path, None)
                    except Exception: pass
                    _invalidate_dashboard_cache()

    _, months_meta = load_month_baselines()
    uploaded = {m["month"]: m for m in months_meta}
    rows = []
    for ym in _slots_to_show():
        if ym in uploaded:
            m = uploaded[ym]
            rows.append({"month": ym, "label": m["label"], "end": m["end"], "uploaded": True,
                         "issued": m["issued"], "approved": m["approved"],
                         "villages": m["villages"], "error": m.get("error")})
        else:
            rows.append({"month": ym, "label": _month_label(ym), "end": _month_end_date(ym),
                         "uploaded": False, "issued": 0, "approved": 0, "villages": 0, "error": None})
    for m in months_meta:
        if m["month"] not in [r["month"] for r in rows]:
            rows.append({"month": m["month"], "label": m["label"], "end": m["end"],
                         "uploaded": True, "issued": m["issued"], "approved": m["approved"],
                         "villages": m["villages"], "error": m.get("error")})

    totals = {"issued": sum(r["issued"] for r in rows if r["uploaded"]),
              "approved": sum(r["approved"] for r in rows if r["uploaded"]),
              "months": sum(1 for r in rows if r["uploaded"])}

    snaps = discover_snapshots()
    current_month_info = None
    if snaps:
        latest_date, _ = snaps[-1]
        current_month_info = {"latest_date": latest_date, "ym": latest_date[:7],
                              "label": _month_label(latest_date[:7]), "count": len(snaps)}

    return render_template("month_baselines.html",
                           rows=rows, totals=totals, current_month=current_month_info,
                           baseline_date=effective_baseline_date(),
                           baseline_label=effective_baseline_label(),
                           error=error, saved=saved)

# =========================================================================
# Targets admin — tehsil-wise list with per-row tehsil dropdown; target=0 hides
# =========================================================================

@app.route("/admin/targets", methods=["GET", "POST"])
@admin_required
def edit_targets():
    error = None; saved = False

    if request.method == "POST":
        new_targets = {}
        new_tehsil_overrides = {}
        new_unbucketed = {}
        # Row edits: target::<norm_key> and tehsil::<norm_key>
        for key, val in request.form.items():
            if key.startswith("target::"):
                vk = key[len("target::"):]
                try: n = int(val or 0)
                except ValueError: n = 0
                if n < 0: n = 0
                new_targets[vk] = n
        for key, val in request.form.items():
            if key.startswith("tehsil::"):
                vk = key[len("tehsil::"):]
                t = (val or "").strip()
                # v37.2 — store EVERY explicit tehsil pick, regardless of whether
                # it matches master. Because CSV tehsil now auto-wins over master,
                # the admin's pick must always stick to prevent CSV from clobbering it.
                if t and t in KUPWARA_TEHSILS:
                    new_tehsil_overrides[vk] = t
        for key, val in request.form.items():
            if key.startswith("unbucketed::") and val == "yes":
                vk = key[len("unbucketed::"):]
                new_unbucketed[vk] = True

        # Unknown-village new rows: unk_tehsil::<norm_key> and unk_target::<norm_key>
        for key, val in request.form.items():
            if key.startswith("unk_target::"):
                vk = key[len("unk_target::"):]
                t = (request.form.get("unk_tehsil::" + vk) or "").strip()
                try: n = int(val or 0)
                except ValueError: n = 0
                if n < 0: n = 0
                if n > 0:
                    new_targets[vk] = n
                if t and t in KUPWARA_TEHSILS:
                    new_tehsil_overrides[vk] = t

        _write_json(TARGETS_OVERRIDE_FILE, new_targets)
        _write_json(VILLAGE_TEHSIL_OVERRIDE_FILE, new_tehsil_overrides)
        _write_json(UNBUCKETED_FILE, new_unbucketed)

        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
            try:
                commit_to_github(TARGETS_OVERRIDE_FILE, "data/targets.json",
                                 "admin: update village targets")
                commit_to_github(VILLAGE_TEHSIL_OVERRIDE_FILE, "data/village_tehsil_overrides.json",
                                 "admin: update village tehsil assignments")
                commit_to_github(UNBUCKETED_FILE, "data/unbucketed.json",
                                 "admin: update unbucketed flags")
                saved = True
            except Exception as e:
                error = "Saved locally but GitHub push failed: {}".format(e)
        else:
            saved = True
        _invalidate_dashboard_cache()

    # Build display state
    overrides         = load_targets_overrides()
    tehsil_overrides  = load_village_tehsil_overrides()
    unbucketed_flags  = load_unbucketed_flags()

    # v37.2 — snapshot the most-recent CSV-reported tehsil for each village.
    # CSV tehsil wins over master tehsil unless admin set a manual override.
    csv_tehsil_by_key = {}
    for _, path in list_month_baseline_files() + discover_snapshots():
        try:
            snap = parse_snapshot_csv(path)
        except Exception:
            continue
        for k, v in snap.items():
            t = canonical_tehsil(v["tehsil"])
            if t in KUPWARA_TEHSILS:
                csv_tehsil_by_key[k] = t

    # Group villages by effective tehsil (CSV / overrides applied)
    rows_by_tehsil = {t: [] for t in KUPWARA_TEHSILS}
    seen_keys = set()
    for t, v, tg in DEFAULT_TARGETS:
        k = norm_key(t, v)
        seen_keys.add(k)
        cur_target = overrides.get(k, tg)
        try: cur_target = int(cur_target)
        except: cur_target = tg
        master_t = canonical_tehsil(t)
        # Priority: admin override > CSV tehsil > master tehsil
        eff_tehsil = tehsil_overrides.get(k,
                        csv_tehsil_by_key.get(k, master_t))
        rows_by_tehsil.setdefault(eff_tehsil, []).append({
            "key": k,
            "village": v,
            "master_tehsil": canonical_tehsil(t),
            "effective_tehsil": eff_tehsil,
            "default_target": tg,
            "current_target": cur_target,
            "target_overridden": cur_target != tg,
            "unbucketed_flag": unbucketed_flags.get(k, False),
            "unbucketed": unbucketed_flags.get(k, False) or (cur_target or 0) <= 0,
            "from_master": True,
        })

    # Admin-added villages (overrides keys not in master list)
    for k, cur in overrides.items():
        if k in seen_keys: continue
        parts = k.split("||", 1)
        t_raw = parts[0] if len(parts) == 2 else ""
        v = parts[1].title() if len(parts) == 2 else k.lstrip("|").title()
        try: cur = int(cur)
        except: cur = 0
        master_t_guess = canonical_tehsil(t_raw) if t_raw else ""
        eff_tehsil = tehsil_overrides.get(k,
                        csv_tehsil_by_key.get(k, master_t_guess))
        rows_by_tehsil.setdefault(eff_tehsil, []).append({
            "key": k, "village": v,
            "master_tehsil": None, "effective_tehsil": eff_tehsil,
            "default_target": None, "current_target": cur,
            "target_overridden": True,
            "unbucketed_flag": unbucketed_flags.get(k, False),
            "unbucketed": unbucketed_flags.get(k, False) or cur <= 0,
            "from_master": False,
        })
        seen_keys.add(k)

    # Sort inside each tehsil by village name
    for t in rows_by_tehsil:
        rows_by_tehsil[t].sort(key=lambda r: r["village"].upper())

    # Counts per tehsil
    tehsil_rows = []
    for t in KUPWARA_TEHSILS + [x for x in rows_by_tehsil.keys() if x not in KUPWARA_TEHSILS]:
        rows = rows_by_tehsil.get(t, [])
        if not rows and t not in KUPWARA_TEHSILS: continue
        bucketed = sum(1 for r in rows if not r["unbucketed"])
        unbucketed = len(rows) - bucketed
        tehsil_rows.append({
            "tehsil": t, "rows": rows,
            "count": len(rows), "bucketed": bucketed, "unbucketed": unbucketed,
            "target_sum": sum(r["current_target"] for r in rows if not r["unbucketed"]),
        })

    # Totals banner
    total_rows   = sum(len(tr["rows"]) for tr in tehsil_rows)
    total_bkt    = sum(tr["bucketed"] for tr in tehsil_rows)
    total_unbkt  = sum(tr["unbucketed"] for tr in tehsil_rows)
    total_target = sum(tr["target_sum"] for tr in tehsil_rows)

    # CSV-unknown section: villages seen in data but not in master list AND not in overrides
    unknowns = []
    for k, info in villages_seen_in_uploads().items():
        if k in seen_keys: continue
        unknowns.append({"key": k, "tehsil_seen": info["tehsil"], "village": info["village"]})
    unknowns.sort(key=lambda r: (r["tehsil_seen"], r["village"]))

    return render_template("edit_targets.html",
                           tehsil_rows=tehsil_rows,
                           tehsils=KUPWARA_TEHSILS,
                           total_rows=total_rows, total_bucketed=total_bkt,
                           total_unbucketed=total_unbkt, total_target=total_target,
                           unknowns=unknowns,
                           error=error, saved=saved)

# =========================================================================
# CSV village reconciliation (spelling drift)
# =========================================================================

@app.route("/admin/csv-reconciliation", methods=["GET", "POST"])
@admin_required
def csv_reconciliation():
    error = None; saved = False

    if request.method == "POST":
        # Build new alias map by overlaying existing + form choices
        new_aliases = dict(_load_csv_village_aliases())
        for key, val in request.form.items():
            if not key.startswith("map::"): continue
            csv_norm = key[len("map::"):]
            target = (val or "").strip()
            if target:
                new_aliases[csv_norm] = target
            elif csv_norm in new_aliases:
                # Blank selection -> clear mapping
                del new_aliases[csv_norm]
        _write_json(CSV_ALIAS_FILE, new_aliases)
        _invalidate_csv_alias_cache()
        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
            try:
                commit_to_github(CSV_ALIAS_FILE, "data/csv_village_aliases.json",
                                 "admin: update CSV village aliases")
                saved = True
            except Exception as e:
                error = "Saved locally but GitHub push failed: {}".format(e)
        else:
            saved = True
        _invalidate_dashboard_cache()

    # Build list of CSV-seen village norms that don't match any master village
    master_village_norms = set()
    master_villages_by_norm = {}
    for t, v, _ in DEFAULT_TARGETS:
        n = norm_name(v)
        master_village_norms.add(n)
        master_villages_by_norm[n] = (canonical_tehsil(t), v)
    aliases = _load_csv_village_aliases()

    seen = {}  # norm -> {display_raw, canonical_tehsil}
    for _, path in list_month_baseline_files() + discover_snapshots():
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                reader = csv.reader(f); header = next(reader, None)
                if not header: continue
                t_idx = find_header_idx(header, TEHSIL_HEADERS)
                v_idx = find_header_idx(header, VILLAGE_HEADERS)
                if t_idx < 0 or v_idx < 0: continue
                for row in reader:
                    if not row or len(row) <= max(t_idx, v_idx): continue
                    raw_t = (row[t_idx] or "").strip()
                    raw_v = (row[v_idx] or "").strip()
                    if not raw_v: continue
                    n = norm_name(raw_v)
                    if n not in seen:
                        seen[n] = {"raw_village": raw_v, "raw_tehsil": raw_t}
        except Exception:
            continue

    unmapped = []
    mapped = []
    for n, info in sorted(seen.items()):
        mapped_to = aliases.get(n)
        if mapped_to:
            mapped.append({"csv_norm": n, "raw_village": info["raw_village"],
                           "raw_tehsil": info["raw_tehsil"], "mapped_to": mapped_to})
        elif n not in master_village_norms:
            unmapped.append({"csv_norm": n, "raw_village": info["raw_village"],
                             "raw_tehsil": info["raw_tehsil"]})

    # Candidate master villages for the dropdown
    candidates = sorted(master_villages_by_norm.keys())

    return render_template("csv_reconciliation.html",
                           unmapped=unmapped, mapped=mapped,
                           candidates=candidates,
                           master_villages_by_norm=master_villages_by_norm,
                           error=error, saved=saved)

# =========================================================================
# Downloads
# =========================================================================

def _pct(num, den):
    return round((num / den * 100), 1) if den > 0 else 0.0

@app.route("/download/csv")
def download_csv():
    import io
    state = build_nested_structure()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Tehsil", "Village", "Target", "Total Issued",
                "Additions this month", "Issue %", "Approved", "Approval %"])
    for t in state["tehsils"]:
        for v in t["villages"]:
            additions = v["issued"] - v["issuedBaseline"]
            w.writerow([t["tehsil"], v["village"], v["target"], v["issued"],
                        additions, _pct(v["issued"], v["target"]),
                        v["approved"], _pct(v["approved"], v["issued"])])
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=kupwara_farmer_id_{today}.csv"})

@app.route("/download/xlsx")
def download_xlsx():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    state = build_nested_structure()
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1A3A5C")
    totals_font = Font(bold=True)
    totals_fill = PatternFill("solid", fgColor="EEF2F7")

    def write_sheet(ws, headers, rows, totals_row=None):
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=i, value=h)
            c.font = header_font; c.fill = header_fill
            c.alignment = Alignment(horizontal="left", vertical="center")
        for r_idx, row in enumerate(rows, 2):
            for c_idx, val in enumerate(row, 1):
                ws.cell(row=r_idx, column=c_idx, value=val)
        if totals_row:
            r = len(rows) + 2
            for c_idx, val in enumerate(totals_row, 1):
                cell = ws.cell(row=r, column=c_idx, value=val)
                cell.font = totals_font; cell.fill = totals_fill
        for c_idx in range(1, len(headers) + 1):
            max_len = max([len(str(headers[c_idx-1]))] + [len(str(r[c_idx-1])) for r in rows if c_idx-1 < len(r)])
            ws.column_dimensions[get_column_letter(c_idx)].width = min(max_len + 2, 42)
        ws.freeze_panes = "A2"

    # Sheet 1: Tehsil Summary
    ws = wb.active; ws.title = "Tehsil Summary"
    headers = ["#", "Tehsil", "Villages", "Target", "Total Issued",
               "Additions this month", "Issue %", "Approved", "Approval %"]
    rows = []; gT=gI=gA=gAdd=0
    for i, t in enumerate(sorted(state["tehsils"], key=lambda t: -t["issued"]), 1):
        additions = t["issued"] - t["issuedBaseline"]
        rows.append([i, t["tehsil"], len(t["villages"]), t["target"], t["issued"],
                     additions, _pct(t["issued"], t["target"]),
                     t["approved"], _pct(t["approved"], t["issued"])])
        gT += t["target"]; gI += t["issued"]; gA += t["approved"]; gAdd += additions
    totals = ["", "TOTAL", "", gT, gI, gAdd, _pct(gI, gT), gA, _pct(gA, gI)]
    write_sheet(ws, headers, rows, totals)

    # Sheet 2: Village Details
    ws = wb.create_sheet("Village Details")
    headers = ["#", "Tehsil", "Village", "Target", "Total Issued",
               "Additions this month", "Issue %", "Approved", "Approval %"]
    rows = []; n = 1
    for t in state["tehsils"]:
        for v in t["villages"]:
            additions = v["issued"] - v["issuedBaseline"]
            rows.append([n, t["tehsil"], v["village"], v["target"], v["issued"],
                         additions, _pct(v["issued"], v["target"]),
                         v["approved"], _pct(v["approved"], v["issued"])])
            n += 1
    write_sheet(ws, headers, rows)

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(buf.getvalue(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=kupwara_farmer_id_{today}.xlsx"})

# =========================================================================
# Analysis
# =========================================================================

@app.route("/analysis")
def analysis():
    state = build_nested_structure()
    villages = []
    for t in state["tehsils"]:
        for v in t["villages"]:
            additions = v["issued"] - v["issuedBaseline"]
            villages.append({
                "tehsil": t["tehsil"], "village": v["village"],
                "target": v["target"], "issued": v["issued"],
                "approved": v["approved"], "additions": additions,
                "issuePct": _pct(v["issued"], v["target"]),
                "approvalPct": _pct(v["approved"], v["issued"]),
            })
    with_target = [v for v in villages if v["target"] > 0]
    top10 = sorted(with_target, key=lambda v: -v["issuePct"])[:10]
    bottom10 = sorted(with_target, key=lambda v: v["issuePct"])[:10]
    no_progress = [v for v in with_target if v["additions"] == 0]
    overachievers = [v for v in villages if v["issuePct"] >= 100]
    at_risk = sorted([v for v in with_target if v["issuePct"] < 30], key=lambda v: v["issuePct"])

    tehsil_ranking = []
    for t in state["tehsils"]:
        tehsil_ranking.append({
            "tehsil": t["tehsil"], "villages": len(t["villages"]),
            "target": t["target"], "issued": t["issued"], "approved": t["approved"],
            "additions": t["issued"] - t["issuedBaseline"],
            "issuePct": _pct(t["issued"], t["target"]),
            "approvalPct": _pct(t["approved"], t["issued"]),
        })
    tehsil_ranking.sort(key=lambda r: -r["issuePct"])

    tot_tgt = sum(v["target"] for v in villages)
    tot_iss = sum(v["issued"] for v in villages)
    tot_app = sum(v["approved"] for v in villages)
    tot_add = sum(v["additions"] for v in villages)

    data = {
        "summary": {
            "count": len(villages),
            "target": tot_tgt, "issued": tot_iss, "approved": tot_app, "additions": tot_add,
            "issuePct": _pct(tot_iss, tot_tgt), "approvalPct": _pct(tot_app, tot_iss),
        },
        "top10": top10, "bottom10": bottom10,
        "no_progress": no_progress, "overachievers": overachievers, "at_risk": at_risk,
        "tehsil_ranking": tehsil_ranking,
        "state_meta": {
            "baselineDate": state["baselineDate"], "baselineLabel": state["baselineLabel"],
            "snapshotDates": state["snapshotDates"], "generated": state["generated"],
        },
    }
    return render_template("analysis.html", data=data)

# =========================================================================
# GitHub commit (deduped by content SHA)
# =========================================================================

def commit_to_github(local_path, repo_path, message):
    import base64, hashlib, urllib.request, urllib.error
    token = os.environ["GH_TOKEN"]
    repo = os.environ["GH_REPO"]
    branch = os.environ.get("GH_BRANCH", "main")
    url = "https://api.github.com/repos/{}/contents/{}".format(repo, repo_path)
    with open(local_path, "rb") as f:
        local_bytes = f.read()
    content_b64 = base64.b64encode(local_bytes).decode("ascii")
    sha = None
    try:
        req = urllib.request.Request(url + "?ref=" + branch,
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/vnd.github+json"})
        resp = urllib.request.urlopen(req, timeout=15)
        sha = json.loads(resp.read())["sha"]
        header = ("blob %d\0" % len(local_bytes)).encode("latin-1")
        if hashlib.sha1(header + local_bytes).hexdigest() == sha:
            return
    except urllib.error.HTTPError as e:
        if e.code != 404: raise
    body = {"message": message, "content": content_b64, "branch": branch}
    if sha: body["sha"] = sha
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Authorization": "Bearer " + token,
                                          "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json"},
                                 method="PUT")
    urllib.request.urlopen(req, timeout=30)

# =========================================================================
# Main
# =========================================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
