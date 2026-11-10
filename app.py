"""
Kupwara Farmer Enrollment Portal — v40 (simplified, CSV-authoritative)
======================================================================
Flask app for Render deployment.

Data model
----------
* Master list = AGRISTACK monthly CSVs (Aug + Sep + Oct 2026). Only those
  (tehsil, village) pairs are shown on the portal. All start at target=0 /
  pendency=0 — admin sets them from the Targets page.
* Village-name normalization is hard (strip all whitespace/punctuation) so
  "Lal Pora" and "Lalpora" always match the same row.
* A village appears on the dashboard IF it is in the master list OR has
  been added by admin AND it has NOT been deleted. Target zero is fine.
* Admin can ADD a village (comes in via the Targets page or a daily upload
  confirmation flow) and DELETE a village (hidden from dashboard).

Daily upload flow
-----------------
On upload, the server checks each (tehsil, village) in the CSV against the
master + added lists. If any are new, the upload is HELD in a pending state
and the admin is asked to confirm a tehsil + target + pendency for each new
village before the upload is finalized and committed.
"""
import csv
import glob
import json
import os
import re
import secrets as _secrets
import shutil
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, Response, abort, render_template, request, jsonify, session, redirect, url_for, flash

# =========================================================================
# Baked-in: tehsil list + master (tehsil, village) pairs from CSVs
# =========================================================================

PORTAL_TITLE = "Kupwara District — Farmer Enrollment Dashboard"
PORTAL_SHORT = "Kupwara Farmer ID Progress"
DISTRICT_NAME = "Kupwara"

DEFAULT_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
DEFAULT_ADMIN_PASS = os.environ.get("ADMIN_PASS", "Password.1719@")

from default_targets import KUPWARA_TEHSILS, DEFAULT_TARGETS

_BASELINE_FALLBACK_DATE  = "2026-07-31"
_BASELINE_FALLBACK_LABEL = "no baseline uploaded yet"

# =========================================================================
# Paths
# =========================================================================

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
MONTH_BASELINES_DIR    = os.path.join(DATA_DIR, "baselines")
PENDING_UPLOADS_DIR    = os.path.join(DATA_DIR, "pending")
TARGETS_OVERRIDE_FILE  = os.path.join(DATA_DIR, "targets.json")
PENDENCY_OVERRIDE_FILE = os.path.join(DATA_DIR, "pendency.json")
ADDED_VILLAGES_FILE    = os.path.join(DATA_DIR, "added_villages.json")
DELETED_VILLAGES_FILE  = os.path.join(DATA_DIR, "deleted_villages.json")

# =========================================================================
# Flask
# =========================================================================

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", _secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# =========================================================================
# Normalization
# =========================================================================

def norm_name(s):
    """Hard-normalize a name: strip ALL whitespace and punctuation so spelling
    drift ("Lal Pora" vs "Lalpora") naturally matches the same key."""
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())

_TEHSIL_CANONICAL = {norm_name(t): t for t in KUPWARA_TEHSILS}

def canonical_tehsil(t):
    n = norm_name(t)
    return _TEHSIL_CANONICAL.get(n, t)

def norm_key(tehsil, village):
    """Composite (tehsil, village) key. Same village name under different
    tehsils is always kept separate — tehsil + hard-normed village name."""
    return canonical_tehsil(tehsil).upper() + "||" + norm_name(village)

# =========================================================================
# JSON overrides
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

def load_pendency_overrides():
    return _read_json(PENDENCY_OVERRIDE_FILE, {})

def load_added_villages():
    """{norm_key: {tehsil, village, target, pendency}}"""
    return _read_json(ADDED_VILLAGES_FILE, {})

def load_deleted_villages():
    """{norm_key: true}"""
    return _read_json(DELETED_VILLAGES_FILE, {})

# =========================================================================
# CSV parsing
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

_snapshot_parse_cache = {}

def parse_date_from_filename(path):
    name = os.path.splitext(os.path.basename(path))[0]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$|^(\d{2})[.\-](\d{2})[.\-](\d{4})$", name)
    if not m: return None
    g = m.groups()
    return "{}-{}-{}".format(g[0], g[1], g[2]) if g[0] else "{}-{}-{}".format(g[5], g[4], g[3])

def parse_snapshot_csv(path):
    """Return {village_key: {issued, approved, tehsil, village}}."""
    counts = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        try: header = next(reader)
        except StopIteration: return counts
        t_idx = find_header_idx(header, TEHSIL_HEADERS)
        v_idx = find_header_idx(header, VILLAGE_HEADERS)
        s_idx = find_header_idx(header, STATUS_HEADERS)
        if t_idx < 0 or v_idx < 0 or s_idx < 0:
            raise ValueError("{}: header must have tehsil, village and status columns".format(os.path.basename(path)))
        for row in reader:
            if not row or len(row) <= max(t_idx, v_idx, s_idx): continue
            tehsil = (row[t_idx] or "").strip()
            village = (row[v_idx] or "").strip()
            if not tehsil or not village: continue
            key = norm_key(tehsil, village)
            rec = counts.setdefault(key, {"tehsil": tehsil, "village": village, "issued": 0, "approved": 0})
            rec["issued"] += 1
            if normalize_status(row[s_idx]) == "APPROVED":
                rec["approved"] += 1
    return counts

def villages_in_csv(path):
    """Return a set of norm_keys present in the CSV. Lightweight for new-village detection."""
    seen = set()
    display = {}
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if not header: return seen, display
        t_idx = find_header_idx(header, TEHSIL_HEADERS)
        v_idx = find_header_idx(header, VILLAGE_HEADERS)
        if t_idx < 0 or v_idx < 0: return seen, display
        for row in reader:
            if not row or len(row) <= max(t_idx, v_idx): continue
            tehsil = (row[t_idx] or "").strip()
            village = (row[v_idx] or "").strip()
            if not tehsil or not village: continue
            k = norm_key(tehsil, village)
            if k not in seen:
                seen.add(k)
                display[k] = {"tehsil": tehsil, "village": village}
    return seen, display

# =========================================================================
# Monthly baselines
# =========================================================================

_MONTH_FILE_RE = re.compile(r"^(\d{4})-(\d{2})\.csv$")

def _month_end_date(ym):
    y, m = ym.split("-"); y = int(y); m = int(m)
    nxt = datetime(y+1, 1, 1) if m == 12 else datetime(y, m+1, 1)
    return (nxt - timedelta(days=1)).strftime("%Y-%m-%d")

def _month_label(ym):
    try: return datetime.strptime(ym + "-01", "%Y-%m-%d").strftime("%B %Y")
    except: return ym

def list_month_baseline_files():
    out = []
    if not os.path.isdir(MONTH_BASELINES_DIR): return out
    for path in sorted(glob.glob(os.path.join(MONTH_BASELINES_DIR, "*.csv"))):
        m = _MONTH_FILE_RE.match(os.path.basename(path))
        if m: out.append(("{}-{}".format(m.group(1), m.group(2)), path))
    out.sort(key=lambda x: x[0])
    return out

def load_month_baselines():
    sum_counts = {}
    months_meta = []
    for ym, path in list_month_baseline_files():
        try:
            mt = os.path.getmtime(path)
            cached = _snapshot_parse_cache.get(path)
            if cached and cached[0] == mt: snap = cached[1]
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
    return _month_end_date(files[-1][0]) if files else _BASELINE_FALLBACK_DATE

def effective_baseline_label():
    files = list_month_baseline_files()
    if not files: return _BASELINE_FALLBACK_LABEL
    return "through " + datetime.strptime(effective_baseline_date(), "%Y-%m-%d").strftime("%d %b %Y")

def baseline_counts():
    sum_counts, _ = load_month_baselines()
    return {k: {"tehsil": v["tehsil"], "village": v["village"],
                "issued": v["issued"], "approved": v["approved"]}
            for k, v in sum_counts.items()}

# =========================================================================
# Daily snapshots + cumulative state
# =========================================================================

def discover_snapshots():
    snaps = []
    if not os.path.isdir(DATA_DIR): return snaps
    cutoff = effective_baseline_date()
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        d = parse_date_from_filename(path)
        if d and d > cutoff:
            snaps.append((d, path))
    snaps.sort(key=lambda x: x[0])
    return snaps

def _merge(dst, src):
    for k, v in src.items():
        prev = dst.get(k, {})
        dst[k] = {
            "tehsil":   prev.get("tehsil", v["tehsil"]),
            "village":  prev.get("village", v["village"]),
            "issued":   max(v["issued"],   prev.get("issued", 0)),
            "approved": max(v["approved"], prev.get("approved", 0)),
        }

def build_state():
    """Baseline + current-month running max, summed per village (v37.1 model)."""
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
            if cached and cached[0] == mt: snap = cached[1]
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
# Effective master list
# =========================================================================

def effective_master():
    """Yield (key, tehsil, village, target, pendency, source) for the full
    master list: CSV-derived villages + admin-added, minus admin-deleted.
    source = 'csv' or 'added'."""
    targets_o   = load_targets_overrides()
    pendency_o  = load_pendency_overrides()
    added       = load_added_villages()
    deleted     = load_deleted_villages()
    seen = set()
    for t, v, tg_default, pd_default in DEFAULT_TARGETS:
        k = norm_key(t, v)
        if k in deleted: continue
        seen.add(k)
        tg = int(targets_o.get(k, tg_default) or 0)
        pd = int(pendency_o.get(k, pd_default) or 0)
        yield (k, canonical_tehsil(t), v, tg, pd, "csv")
    for k, info in added.items():
        if k in seen or k in deleted: continue
        t = info.get("tehsil", "")
        v = info.get("village", "")
        tg = int(targets_o.get(k, info.get("target", 0)) or 0)
        pd = int(pendency_o.get(k, info.get("pendency", 0)) or 0)
        yield (k, canonical_tehsil(t), v, tg, pd, "added")
        seen.add(k)

# =========================================================================
# Dashboard build
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
    baseline_by_key = baseline_counts()

    # Build villages dict from MASTER LIST ONLY. Every master village appears,
    # regardless of target or issued. Attached CSV counts (cumulative) are the
    # farmer-ID totals; villages without CSV data get zero counts.
    villages = {}
    for k, tehsil, vname, tg, pd, source in effective_master():
        rec = cumulative.get(k)
        iss_b = baseline_by_key.get(k, {}).get("issued", 0)
        app_b = baseline_by_key.get(k, {}).get("approved", 0)
        villages[k] = {
            "tehsil": tehsil, "village": vname,
            "target": tg, "pendency": pd,
            "issued":   rec["issued"]   if rec else iss_b,
            "approved": rec["approved"] if rec else app_b,
            "issuedBaseline": iss_b, "approvedBaseline": app_b,
            "source": source,
        }

    # Per-village dated counts (positive deltas each day)
    daily = per_day_additions(per_date, sorted_dates)
    for d, data in daily.items():
        for vk, delta in data["by_village"].items():
            if vk in villages and delta > 0:
                villages[vk].setdefault("datedCounts", {})
                slot = villages[vk]["datedCounts"].setdefault(d, {"issued": 0, "approved": 0})
                slot["issued"] += delta
    for i, d in enumerate(sorted_dates):
        current = per_date[d]
        for key, rec in current.items():
            if key not in villages: continue
            if i == 0: a_delta = rec["approved"]
            else:
                prev_d = per_date[sorted_dates[i-1]]
                a_delta = rec["approved"] - prev_d.get(key, {}).get("approved", 0)
            if a_delta > 0:
                villages[key].setdefault("datedCounts", {})
                slot = villages[key]["datedCounts"].setdefault(d, {"issued": 0, "approved": 0})
                slot["approved"] += a_delta
    for v in villages.values():
        v.setdefault("datedCounts", {})

    # Group by tehsil
    tehsils = {t: {"tehsil": t, "target": 0, "pendency": 0, "issued": 0, "approved": 0,
                   "issuedBaseline": 0, "approvedBaseline": 0, "villages": []}
               for t in KUPWARA_TEHSILS}
    for key, v in villages.items():
        t = canonical_tehsil(v["tehsil"])
        if t not in tehsils:
            tehsils[t] = {"tehsil": t, "target": 0, "pendency": 0, "issued": 0, "approved": 0,
                          "issuedBaseline": 0, "approvedBaseline": 0, "villages": []}
        obj = tehsils[t]
        obj["target"]           += v["target"]
        obj["pendency"]         += v.get("pendency", 0)
        obj["issued"]           += v["issued"]
        obj["approved"]         += v["approved"]
        obj["issuedBaseline"]   += v["issuedBaseline"]
        obj["approvedBaseline"] += v["approvedBaseline"]
        obj["villages"].append(v)

    for t in tehsils.values():
        t["villages"].sort(key=lambda v: (-(v["issued"]/v["target"]) if v["target"] > 0 else -2,
                                          v["village"].upper()))
    tehsil_list = [t for t in tehsils.values() if t["villages"]]
    tehsil_list.sort(key=lambda t: -t["issued"])

    _baseline_date = effective_baseline_date()
    _baseline_label = effective_baseline_label()
    recent_dates = [d for d in sorted_dates if d > _baseline_date][-5:]
    last5 = [{"date": d, "total": daily[d]["total"]} for d in recent_dates]

    result = {
        "tehsils": tehsil_list, "last5": last5,
        "minDate": sorted_dates[0] if sorted_dates else _baseline_date,
        "maxDate": sorted_dates[-1] if sorted_dates else _baseline_date,
        "baselineDate": _baseline_date, "baselineLabel": _baseline_label,
        "generated": datetime.utcnow().isoformat() + "Z",
        "snapshotDates": [d for d in sorted_dates if d > _baseline_date],
        "errors": errors, "portalTitle": PORTAL_TITLE, "districtName": DISTRICT_NAME,
    }
    _dashboard_cache.update({"ts": now, "data": result})
    return result

# =========================================================================
# Login / admin
# =========================================================================

def _is_admin(): return bool(session.get("admin"))

@app.context_processor
def _ctx():
    return {"is_admin": _is_admin(), "portal_title": PORTAL_TITLE, "portal_short": PORTAL_SHORT}

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
                           tehsils=KUPWARA_TEHSILS, n_villages=len(DEFAULT_TARGETS))

# =========================================================================
# Dashboard + utilities
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
    try: return datetime.strptime(ymd[:10], "%Y-%m-%d").strftime("%d %b '%y")
    except: return ymd

# =========================================================================
# Daily upload + new-village confirmation flow
# =========================================================================

def _parse_date_input(raw):
    raw = (raw or "").strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m: return raw
    m = re.match(r"^(\d{2})[./-](\d{2})[./-](\d{4})$", raw)
    if m: return "{}-{}-{}".format(m.group(3), m.group(2), m.group(1))
    return None

def _known_master_keys():
    """Set of norm_keys currently in the master list (CSV + added, minus deleted)."""
    return {k for k, *_ in effective_master()}

@app.route("/upload", methods=["GET", "POST"])
@admin_required
def upload():
    error = None; saved = None; replaced_date = None
    baseline_date = effective_baseline_date()
    pending_info = None   # non-None → render the "confirm new villages" form

    if request.method == "POST":
        action = (request.form.get("action") or "upload").strip()

        # ---- Finalize a pending upload after admin filled in new-village info ----
        if action == "confirm_pending":
            return _finalize_pending_upload()

        # ---- Cancel a pending upload ----
        if action == "cancel_pending":
            return _cancel_pending_upload()

        # ---- Normal upload: parse, check for new villages, maybe stage ----
        date = _parse_date_input(request.form.get("date", ""))
        if not date or date <= baseline_date:
            error = ("Date must be in dd-mm-yyyy and after {} (the latest closed-month "
                     "baseline).").format(baseline_date)
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
                    # Stage the upload first, then parse for new villages
                    os.makedirs(PENDING_UPLOADS_DIR, exist_ok=True)
                    pending_path = os.path.join(PENDING_UPLOADS_DIR, date + ".csv")
                    f.save(pending_path)
                    try:
                        parse_snapshot_csv(pending_path)  # validate
                    except Exception as e:
                        try: os.remove(pending_path)
                        except OSError: pass
                        error = "That CSV didn't parse: {}".format(e)
                    else:
                        keys_in_csv, display = villages_in_csv(pending_path)
                        known = _known_master_keys()
                        new_keys = [k for k in keys_in_csv if k not in known]
                        if new_keys:
                            # Stash the metadata so the confirmation form knows
                            # which pending file + date to work with.
                            session["pending_upload"] = {
                                "date": date,
                                "replace": already_exists,
                                "new_keys": new_keys,
                            }
                            pending_info = {
                                "date": date,
                                "replace": already_exists,
                                "new_villages": [{
                                    "key": k,
                                    "tehsil": display[k]["tehsil"],
                                    "village": display[k]["village"],
                                } for k in new_keys],
                            }
                        else:
                            # No new villages — finalize immediately.
                            shutil.move(pending_path, out_path)
                            saved, error = _commit_snapshot(date, out_path, already_exists)
                            if already_exists: replaced_date = date

    existing = [d for d, _ in discover_snapshots()]
    return render_template("upload.html",
                           baseline_date=baseline_date,
                           baseline_label=effective_baseline_label(),
                           existing_dates=existing,
                           replaced_date=replaced_date,
                           pending=pending_info,
                           tehsils=KUPWARA_TEHSILS,
                           error=error, saved=saved)

def _commit_snapshot(date, out_path, already_exists):
    saved = date + ".csv"; error = None
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
    return saved, error

def _finalize_pending_upload():
    """Called when admin submits the new-village confirmation form. Saves the
    villages the admin named, moves the pending CSV into data/, and commits."""
    pend = session.get("pending_upload")
    if not pend:
        flash("No pending upload found — maybe it expired. Please upload again.", "err")
        return redirect(url_for("upload"))
    date       = pend["date"]
    replace    = pend["replace"]
    new_keys   = pend["new_keys"]
    pending_path = os.path.join(PENDING_UPLOADS_DIR, date + ".csv")
    if not os.path.exists(pending_path):
        session.pop("pending_upload", None)
        flash("Pending CSV was lost on disk. Please upload again.", "err")
        return redirect(url_for("upload"))

    added = load_added_villages()
    for k in new_keys:
        tehsil = (request.form.get("new_tehsil::" + k) or "").strip()
        vname  = (request.form.get("new_village::" + k) or "").strip()
        try: tg = max(0, int(request.form.get("new_target::" + k) or 0))
        except: tg = 0
        try: pd = max(0, int(request.form.get("new_pendency::" + k) or 0))
        except: pd = 0
        if not tehsil: tehsil = ""     # best-effort fallback
        if not vname:  vname  = ""
        added[k] = {"tehsil": tehsil, "village": vname, "target": tg, "pendency": pd}
    _write_json(ADDED_VILLAGES_FILE, added)

    out_path = os.path.join(DATA_DIR, date + ".csv")
    try:
        shutil.move(pending_path, out_path)
    except Exception as e:
        flash("Could not move pending CSV into place: {}".format(e), "err")
        return redirect(url_for("upload"))
    saved_msg, err = _commit_snapshot(date, out_path, replace)
    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
        try:
            commit_to_github(ADDED_VILLAGES_FILE, "data/added_villages.json",
                             "admin: add {} new village(s) from {} upload".format(len(new_keys), date))
        except Exception as e:
            err = "Saved snapshot but added_villages commit failed: {}".format(e)

    session.pop("pending_upload", None)
    if err: flash("Upload saved. " + err, "err")
    else:   flash("Upload saved: {} — {} new village(s) added.".format(saved_msg, len(new_keys)), "ok")
    return redirect(url_for("upload"))

def _cancel_pending_upload():
    pend = session.pop("pending_upload", None)
    if pend:
        try: os.remove(os.path.join(PENDING_UPLOADS_DIR, pend["date"] + ".csv"))
        except OSError: pass
    flash("Pending upload cancelled.", "ok")
    return redirect(url_for("upload"))

# =========================================================================
# Monthly Baselines
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
    slots = []; cur = base_start
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
                if not f or not f.filename: error = "Pick a CSV file for {}.".format(ym)
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
            if not re.match(r"^\d{4}-\d{2}$", ym): error = "Bad month."
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
            if not snaps: error = "No daily snapshots to close."
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

    return render_template("month_baselines.html", rows=rows, totals=totals,
                           current_month=current_month_info,
                           baseline_date=effective_baseline_date(),
                           baseline_label=effective_baseline_label(),
                           error=error, saved=saved)

# =========================================================================
# Targets admin
# =========================================================================

def _admin_add_village():
    tehsil  = (request.form.get("new_tehsil")  or "").strip()
    vname   = (request.form.get("new_village") or "").strip()
    try: tg = max(0, int(request.form.get("new_target", 0) or 0))
    except: tg = 0
    try: pd = max(0, int(request.form.get("new_pendency", 0) or 0))
    except: pd = 0
    if not tehsil or tehsil not in KUPWARA_TEHSILS:
        flash("Pick a tehsil.", "err")
        return redirect(url_for("edit_targets"))
    if not vname or len(vname) < 2:
        flash("Village name too short.", "err")
        return redirect(url_for("edit_targets"))
    k = norm_key(tehsil, vname)
    added = load_added_villages(); deleted = load_deleted_villages()
    if k in deleted:
        deleted.pop(k); _write_json(DELETED_VILLAGES_FILE, deleted)
        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
            try: commit_to_github(DELETED_VILLAGES_FILE, "data/deleted_villages.json",
                                  "admin: undelete {} / {}".format(tehsil, vname))
            except Exception: pass
    added[k] = {"tehsil": tehsil, "village": vname, "target": tg, "pendency": pd}
    _write_json(ADDED_VILLAGES_FILE, added)
    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
        try: commit_to_github(ADDED_VILLAGES_FILE, "data/added_villages.json",
                              "admin: add village {} / {}".format(tehsil, vname))
        except Exception as e: flash("Saved locally but GitHub failed: {}".format(e), "err")
    _invalidate_dashboard_cache()
    flash("Added {} / {}.".format(tehsil, vname), "ok")
    return redirect(url_for("edit_targets"))

def _admin_delete_village():
    vk = (request.form.get("delete_key") or "").strip()
    if not vk:
        flash("Missing village key.", "err"); return redirect(url_for("edit_targets"))
    deleted = load_deleted_villages(); deleted[vk] = True
    _write_json(DELETED_VILLAGES_FILE, deleted)
    if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
        try: commit_to_github(DELETED_VILLAGES_FILE, "data/deleted_villages.json",
                              "admin: delete village {}".format(vk))
        except Exception as e: flash("Deleted locally but GitHub failed: {}".format(e), "err")
    _invalidate_dashboard_cache()
    flash("Village deleted.", "ok")
    return redirect(url_for("edit_targets"))

def _admin_restore_village():
    vk = (request.form.get("restore_key") or "").strip()
    if not vk:
        flash("Missing village key.", "err"); return redirect(url_for("edit_targets"))
    deleted = load_deleted_villages()
    if vk in deleted:
        deleted.pop(vk); _write_json(DELETED_VILLAGES_FILE, deleted)
        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
            try: commit_to_github(DELETED_VILLAGES_FILE, "data/deleted_villages.json",
                                  "admin: restore village {}".format(vk))
            except Exception: pass
        _invalidate_dashboard_cache()
    flash("Village restored.", "ok")
    return redirect(url_for("edit_targets"))

@app.route("/admin/targets", methods=["GET", "POST"])
@admin_required
def edit_targets():
    if request.method == "POST":
        action = (request.form.get("action") or "save").strip()
        if action == "add_village":     return _admin_add_village()
        if action == "delete_village":  return _admin_delete_village()
        if action == "restore_village": return _admin_restore_village()

        # Save all target + pendency edits
        new_targets  = {}
        new_pendency = {}
        for key, val in request.form.items():
            if key.startswith("target::"):
                vk = key[len("target::"):]
                try: n = max(0, int(val or 0))
                except: n = 0
                new_targets[vk] = n
            elif key.startswith("pendency::"):
                vk = key[len("pendency::"):]
                try: n = max(0, int(val or 0))
                except: n = 0
                new_pendency[vk] = n
        _write_json(TARGETS_OVERRIDE_FILE,  new_targets)
        _write_json(PENDENCY_OVERRIDE_FILE, new_pendency)
        if os.environ.get("GH_TOKEN") and os.environ.get("GH_REPO"):
            try:
                commit_to_github(TARGETS_OVERRIDE_FILE,  "data/targets.json",
                                 "admin: update village targets")
                commit_to_github(PENDENCY_OVERRIDE_FILE, "data/pendency.json",
                                 "admin: update village pendency")
            except Exception as e:
                flash("Saved locally but GitHub failed: {}".format(e), "err")
        _invalidate_dashboard_cache()
        flash("Targets + pendency saved.", "ok")
        return redirect(url_for("edit_targets"))

    # ---- Build display state ----
    deleted = load_deleted_villages()
    rows_by_tehsil = {t: [] for t in KUPWARA_TEHSILS}
    deleted_rows = []

    for k, tehsil, vname, tg, pd, source in effective_master():
        row = {"key": k, "village": vname, "tehsil": tehsil,
               "target": tg, "pendency": pd, "source": source,
               "unbucketed": (tg or 0) <= 0}
        rows_by_tehsil.setdefault(tehsil, []).append(row)

    # Collect deleted rows for the recycle bin (from master + added)
    added = load_added_villages()
    for t, v, tg_d, pd_d in DEFAULT_TARGETS:
        k = norm_key(t, v)
        if k in deleted:
            targets_o = load_targets_overrides(); pendency_o = load_pendency_overrides()
            tg = int(targets_o.get(k, tg_d) or 0); pd = int(pendency_o.get(k, pd_d) or 0)
            deleted_rows.append({"key": k, "village": v, "tehsil": canonical_tehsil(t),
                                 "target": tg, "pendency": pd, "source": "csv"})
    for k, info in added.items():
        if k in deleted:
            deleted_rows.append({"key": k, "village": info.get("village", ""),
                                 "tehsil": info.get("tehsil", ""),
                                 "target": int(info.get("target", 0) or 0),
                                 "pendency": int(info.get("pendency", 0) or 0),
                                 "source": "added"})

    for t in rows_by_tehsil:
        rows_by_tehsil[t].sort(key=lambda r: r["village"].upper())

    tehsil_rows = []
    for t in KUPWARA_TEHSILS + [x for x in rows_by_tehsil.keys() if x not in KUPWARA_TEHSILS]:
        rows = rows_by_tehsil.get(t, [])
        if not rows and t not in KUPWARA_TEHSILS: continue
        bucketed = sum(1 for r in rows if not r["unbucketed"])
        unbucketed = len(rows) - bucketed
        tehsil_rows.append({"tehsil": t, "rows": rows,
                            "count": len(rows), "bucketed": bucketed, "unbucketed": unbucketed,
                            "target_sum":   sum(r["target"]   for r in rows),
                            "pendency_sum": sum(r["pendency"] for r in rows)})

    total_rows   = sum(len(tr["rows"]) for tr in tehsil_rows)
    total_bkt    = sum(tr["bucketed"] for tr in tehsil_rows)
    total_unbkt  = sum(tr["unbucketed"] for tr in tehsil_rows)
    total_target = sum(tr["target_sum"] for tr in tehsil_rows)
    total_pendency = sum(tr["pendency_sum"] for tr in tehsil_rows)

    return render_template("edit_targets.html",
                           tehsil_rows=tehsil_rows, tehsils=KUPWARA_TEHSILS,
                           total_rows=total_rows, total_bucketed=total_bkt,
                           total_unbucketed=total_unbkt, total_target=total_target,
                           total_pendency=total_pendency,
                           deleted_rows=deleted_rows)

# =========================================================================
# Downloads
# =========================================================================

def _pct(num, den): return round((num / den * 100), 1) if den > 0 else 0.0

@app.route("/download/csv")
@admin_required
def download_csv():
    import io
    state = build_nested_structure()
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["Tehsil", "Village", "Target", "Pendency", "Total Issued",
                "Additions this month", "Issue %", "Approved", "Approval %"])
    for t in state["tehsils"]:
        for v in t["villages"]:
            additions = v["issued"] - v["issuedBaseline"]
            w.writerow([t["tehsil"], v["village"], v["target"], v.get("pendency", 0),
                        v["issued"], additions, _pct(v["issued"], v["target"]),
                        v["approved"], _pct(v["approved"], v["issued"])])
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=kupwara_farmer_id_{today}.csv"})

@app.route("/download/xlsx")
@admin_required
def download_xlsx():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    state = build_nested_structure()
    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="0B4D2B")
    totals_font = Font(bold=True)
    totals_fill = PatternFill("solid", fgColor="EEF4ED")
    def write_sheet(ws, headers, rows, totals_row=None):
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=i, value=h); c.font = header_font; c.fill = header_fill
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

    ws = wb.active; ws.title = "Tehsil Summary"
    headers = ["#", "Tehsil", "Villages", "Target", "Pendency", "Total Issued",
               "Additions this month", "Issue %", "Approved", "Approval %"]
    rows = []; gT=gP=gI=gA=gAdd=0
    for i, t in enumerate(sorted(state["tehsils"], key=lambda t: -t["issued"]), 1):
        adds = t["issued"] - t["issuedBaseline"]; tp = t.get("pendency", 0)
        rows.append([i, t["tehsil"], len(t["villages"]), t["target"], tp, t["issued"],
                     adds, _pct(t["issued"], t["target"]), t["approved"], _pct(t["approved"], t["issued"])])
        gT += t["target"]; gP += tp; gI += t["issued"]; gA += t["approved"]; gAdd += adds
    totals = ["", "TOTAL", "", gT, gP, gI, gAdd, _pct(gI, gT), gA, _pct(gA, gI)]
    write_sheet(ws, headers, rows, totals)

    ws = wb.create_sheet("Village Details")
    headers = ["#", "Tehsil", "Village", "Target", "Pendency", "Total Issued",
               "Additions this month", "Issue %", "Approved", "Approval %"]
    rows = []; n = 1
    for t in state["tehsils"]:
        for v in t["villages"]:
            adds = v["issued"] - v["issuedBaseline"]
            rows.append([n, t["tehsil"], v["village"], v["target"], v.get("pendency", 0),
                         v["issued"], adds, _pct(v["issued"], v["target"]),
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
    data = {
        "summary": {"count": len(villages),
                    "target": sum(v["target"] for v in villages),
                    "issued": sum(v["issued"] for v in villages),
                    "approved": sum(v["approved"] for v in villages),
                    "additions": sum(v["additions"] for v in villages),
                    "issuePct": _pct(sum(v["issued"] for v in villages), sum(v["target"] for v in villages)),
                    "approvalPct": _pct(sum(v["approved"] for v in villages), sum(v["issued"] for v in villages))},
        "top10": top10, "bottom10": bottom10, "at_risk": at_risk,
        "tehsil_ranking": tehsil_ranking,
        "state_meta": {"baselineDate": state["baselineDate"], "baselineLabel": state["baselineLabel"],
                       "snapshotDates": state["snapshotDates"], "generated": state["generated"]},
    }
    return render_template("analysis.html", data=data)

# =========================================================================
# GitHub commit
# =========================================================================

def commit_to_github(local_path, repo_path, message):
    import base64, hashlib, urllib.request, urllib.error
    token = os.environ["GH_TOKEN"]; repo = os.environ["GH_REPO"]
    branch = os.environ.get("GH_BRANCH", "main")
    url = "https://api.github.com/repos/{}/contents/{}".format(repo, repo_path)
    with open(local_path, "rb") as f: local_bytes = f.read()
    content_b64 = base64.b64encode(local_bytes).decode("ascii")
    sha = None
    try:
        req = urllib.request.Request(url + "?ref=" + branch,
                                     headers={"Authorization": "Bearer " + token,
                                              "Accept": "application/vnd.github+json"})
        resp = urllib.request.urlopen(req, timeout=15)
        sha = json.loads(resp.read())["sha"]
        header = ("blob %d\0" % len(local_bytes)).encode("latin-1")
        if hashlib.sha1(header + local_bytes).hexdigest() == sha: return
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
