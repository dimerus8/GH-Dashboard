"""
GH Trading Dashboard — hosted edition (one URL, nothing to install for users)
=============================================================================
This is the "do what the viewer does" server. It holds BOTH keys (Massive +
Databento) and does every data call itself, so anyone you give the URL to just
opens it — no key to enter, no file to download, nothing to run on their side.

What it serves:
  GET  /                -> the dashboard (GH_Trading_Dashboard_hosted.html)
  GET  /api/aggs        -> Massive/Polygon aggregates (server-side key)
  GET  /api/details     -> Massive/Polygon ticker details (market cap)
  POST /api/imbalance   -> Databento 9:25 opening-cross imbalance (server-side key)

Everything can sit behind a simple username/password login (like the viewer's).

ENV / CONFIG (set on the server, never in the HTML):
  MASSIVE_API_KEY     required  (your Massive/Polygon key)
  DATABENTO_API_KEY   optional  (enables the imbalance column)
  DASH_USER           optional  (if set with DASH_PASS, the whole site needs login)
  DASH_PASS           optional
  DATABENTO_DATASET   optional  (default XNAS.ITCH = Nasdaq NOII)

LOCAL TEST:
  pip install -r requirements_hosted.txt
  set MASSIVE_API_KEY / DATABENTO_API_KEY  (env or the *_key.txt files beside this)
  python gh_app.py        ->  http://127.0.0.1:5000

PRODUCTION: run under gunicorn behind nginx — see DEPLOY_hosted.md.
"""

import os
import functools
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, request, jsonify, send_file, Response

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
HERE = Path(__file__).resolve().parent
DASH_FILE = HERE / "GH_Trading_Dashboard_hosted.html"
DEFAULT_DATASET = os.environ.get("DATABENTO_DATASET", "XNAS.ITCH")
MASSIVE_BASES = ["https://api.massive.com", "https://api.polygon.io"]
def norm_side(v):
    """Databento normalizes imbalance side to Ask/Bid. Ask (A) = sell imbalance,
       Bid (B) = buy imbalance. Robust to 'A'/'B'/'Ask'/'Bid'/'S'/'Sell'/'Buy'."""
    s = str(v).strip().upper()
    if s in ("A", "ASK", "S", "SELL"):
        return "Sell"
    if s in ("B", "BID", "BUY"):
        return "Buy"
    return "None"

app = Flask(__name__)


@app.after_request
def _no_cache(resp):
    # Never let the browser or any edge cache the dashboard HTML — always serve fresh.
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp
_dbn_client = None


# ---------- key loading (env var, or a *_key.txt file beside this script) ----------
def _load(name, filename):
    v = os.environ.get(name)
    if v and v.strip():
        return v.strip()
    p = HERE / filename
    try:
        if p.exists():
            s = p.read_text(encoding="utf-8").strip()
            if s:
                return s
    except Exception:
        pass
    return None


def massive_key():
    k = _load("MASSIVE_API_KEY", "massive_key.txt")
    if not k:
        raise RuntimeError("MASSIVE_API_KEY not set (env var or massive_key.txt).")
    return k


def databento_client():
    global _dbn_client
    if _dbn_client is None:
        import databento as db
        k = _load("DATABENTO_API_KEY", "databento_key.txt")
        if not k:
            raise RuntimeError("DATABENTO_API_KEY not set (env var or databento_key.txt).")
        _dbn_client = db.Historical(k)
    return _dbn_client


# ---------- optional basic-auth login over the whole site ----------
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        user, pw = os.environ.get("DASH_USER"), os.environ.get("DASH_PASS")
        if user and pw:
            auth = request.authorization
            if not auth or auth.username != user or auth.password != pw:
                return Response("Login required", 401,
                                {"WWW-Authenticate": 'Basic realm="GH Trading Dashboard"'})
        return fn(*a, **kw)
    return wrapper


# ---------- Massive proxy (server holds the key) ----------
def massive_get(path):
    """GET a Massive path (massive -> polygon fallback). path starts with '/'."""
    key = massive_key()
    sep = "&" if "?" in path else "?"
    last = None
    for base in MASSIVE_BASES:
        try:
            r = requests.get(base + path + f"{sep}apiKey={key}", timeout=40)
            if r.status_code in (401, 403):
                raise RuntimeError("Massive key rejected (401/403).")
            if r.status_code == 404:
                return {"results": [], "_404": True}
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
    raise last or RuntimeError("Massive unreachable")


@app.route("/api/aggs")
@login_required
def api_aggs():
    ticker = (request.args.get("ticker") or "").upper()
    mult = request.args.get("mult", "1")
    span = request.args.get("span", "minute")
    frm = request.args.get("from")
    to = request.args.get("to")
    if not (ticker and frm and to):
        return jsonify({"ok": False, "error": "ticker, from, to required"}), 400
    try:
        path = (f"/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{frm}/{to}"
                f"?adjusted=true&sort=asc&limit=50000")
        out = []
        j = massive_get(path)
        out.extend(j.get("results") or [])
        # follow pagination
        hops = 0
        while j.get("next_url") and hops < 60:
            nu = j["next_url"]
            nu = nu.split("://", 1)[-1]
            nu = "/" + nu.split("/", 1)[1] if "/" in nu else nu
            j = massive_get(nu)
            out.extend(j.get("results") or [])
            hops += 1
        return jsonify({"ok": True, "results": out})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/details")
@login_required
def api_details():
    ticker = (request.args.get("ticker") or "").upper()
    if not ticker:
        return jsonify({"results": None}), 400
    try:
        return jsonify(massive_get(f"/v3/reference/tickers/{ticker}"))
    except Exception as e:
        return jsonify({"results": None, "error": str(e)}), 400


# ---------- Databento imbalance ----------
def et_to_utc(date_str, hh, mm):
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=hh, minute=mm, tzinfo=ET)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M")


def imbalance_for_date(ticker, date_str, dataset):
    start, end = et_to_utc(date_str, 9, 25), et_to_utc(date_str, 9, 30)
    try:
        data = databento_client().timeseries.get_range(
            dataset=dataset, schema="imbalance", symbols=ticker,
            stype_in="raw_symbol", start=start, end=end)
        df = data.to_df()
    except Exception as e:
        return date_str, {"error": str(e)[:160]}
    if df is None or len(df) == 0:
        return date_str, None
    if "ts_event" not in df.columns:
        df = df.reset_index()
    if "auction_type" in df.columns:
        opening = df[df["auction_type"].astype(str).str.upper().str.startswith("O")]
        if len(opening):
            df = opening
    df = df.sort_values("ts_event")

    # Pick the earliest record at/after 9:25 that actually carries an imbalance
    # (a real Buy/Sell side and a positive quantity) — this skips the empty
    # "no imbalance yet" / post-cross padding rows that would otherwise mislead.
    chosen = None
    for _, r in df.iterrows():
        side = norm_side(r.get("side", ""))
        q = r.get("total_imbalance_qty", None)
        try:
            q = None if q is None else int(q)
        except Exception:
            q = None
        if side in ("Buy", "Sell") and q and q > 0:
            chosen = r
            break
    if chosen is None:
        chosen = df.iloc[0]

    side = norm_side(chosen.get("side", ""))
    shares = chosen.get("total_imbalance_qty", None)
    ref = chosen.get("ref_price", None)
    shares = None if shares is None else int(shares)
    ref = None if ref is None else float(ref)
    return date_str, {
        "side": side,
        "shares": shares, "ref_price": ref,
        "notional": None if (shares is None or ref is None) else shares * ref,
        "ts": str(chosen.get("ts_event", "")),
    }


@app.route("/api/imbalance", methods=["POST"])
@login_required
def api_imbalance():
    d = request.get_json(force=True)
    ticker = (d.get("ticker") or "").strip().upper()
    dates = d.get("dates") or []
    dataset = d.get("dataset") or DEFAULT_DATASET
    if not ticker or not dates:
        return jsonify({"ok": False, "error": "ticker and dates required"}), 400
    try:
        databento_client()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    out = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        for date_str, res in ex.map(lambda ds: imbalance_for_date(ticker, ds, dataset), dates):
            out[date_str] = res
    return jsonify({"ok": True, "data": out, "dataset": dataset})


@app.route("/")
@login_required
def index():
    if not DASH_FILE.exists():
        return (f"Dashboard file missing: {DASH_FILE.name} must sit next to gh_app.py", 500)
    return send_file(str(DASH_FILE))


if __name__ == "__main__":
    print("GH Trading Dashboard (hosted) -> http://127.0.0.1:5000")
    print("Massive key:", "yes" if _load("MASSIVE_API_KEY", "massive_key.txt") else "NO")
    print("Databento key:", "yes" if _load("DATABENTO_API_KEY", "databento_key.txt") else "no (imbalance disabled)")
    print("Login:", "ON" if (os.environ.get("DASH_USER") and os.environ.get("DASH_PASS")) else "off")
    app.run(host="0.0.0.0", port=5000, debug=False)
