import os
import re
import csv
import hmac
import json
import time
import datetime
import hashlib
import threading

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)
API_KEY = ""
API_SECRET = ""

# =========================
# ======  CONSTANTS  ======
# =========================

STATE_FILE = "state.json"
TRADES_FILE = "trades.csv"
TRAIL_FILE = "trail_state.json"  # persists watermark+stop per position

LEVERAGE = 5
INITIAL_BALANCE_INR = 5_500.0  # starting balance as requested

# --- Legacy fixed ATR multiplier (kept for reference/compatibility) ---
K_ATR = 2.2

# ===============================
# ===== Dynamic ATR settings =====
# ===============================
# Bounds on allowed ATR multiplier
K_MIN = 1.3      # tightest stop once trade is working & vol is tame
K_MAX = 3.2      # widest stop for choppy/high-vol conditions

# Tighten based on equity return (already levered). Fraction, e.g. 0.015 = 1.5%
ER_TIGHTEN_START = 0.005   # begin tightening after 0.5% equity return
ER_TIGHTEN_END   = 0.05    # fully tight by 5% equity return

# Volatility regime using ATR as % of price (tune to your market)
VOL_LOW  = 0.004   # 0.4% of price → considered low vol
VOL_HIGH = 0.02    # 2.0% of price → considered high vol

# Volatility scaling factors (applied to K chosen by PnL tightening)
VOL_SCALE_MIN = 0.90   # small vol → slightly tighter K
VOL_SCALE_MAX = 1.15   # big vol → widen K modestly


SECRET_BYTES = API_SECRET.encode()

# =========================
# ======  UTILITIES  ======
# =========================

def now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def ensure_trade_log():
    if not os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "timestamp", "action", "side", "price", "qty_btc",
                "entry_price", "exit_price", "pnl_inr",
                "margin_inr", "leverage", "balance_after_inr"
            ])


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                pass
    return {"balance_inr": INITIAL_BALANCE_INR, "position": None, "stopped": False}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def log_trade(action, side, price, qty_btc, entry_price, exit_price, pnl_inr, margin, leverage, balance_after):
    ensure_trade_log()
    with open(TRADES_FILE, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            now_iso(), action, side, price, qty_btc,
            entry_price, exit_price, pnl_inr,
            margin, leverage, balance_after
        ])

# =========================
# ====  TRAIL STORAGE  ====
# =========================

TRAIL_LOCK = threading.Lock()


def _load_trail():
    if os.path.exists(TRAIL_FILE):
        try:
            with open(TRAIL_FILE, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {}


def _save_trail(d):
    tmp = TRAIL_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, TRAIL_FILE)


def _pos_key(state):
    """Build a stable key for the current open position. Prefer a true position_id if you have one."""
    pos = state.get("position")
    if not pos:
        return None
    sym = pos.get("symbol", "B-BTC_USDT")
    side = pos["side"]
    entry = float(pos["entry_price"])
    return f"{sym}:{side}:{entry:.8f}"

# =========================
# =====  PARSING I/O  =====
# =========================

KV_REGEX = re.compile(r'(\b[a-zA-Z_][a-zA-Z0-9_]*\b)\s*=\s*"?([^\s",]+)"?')


def parse_alert(req):
    """
    Returns (side:str, price:float) or (None, None) if invalid.
    Priority: JSON -> form -> raw text "side=buy, price=12345"
    """
    # 1) JSON
    data = req.get_json(silent=True)
    if isinstance(data, dict):
        side = (str(data.get("side") or "")).lower()
        price = data.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        if side in ("buy", "sell") and price is not None:
            return side, price

    # 2) form
    if req.form:
        side = (req.form.get("side") or "").lower()
        price = req.form.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        if side in ("buy", "sell") and price is not None:
            return side, price

    # 3) raw text
    raw = req.data.decode("utf-8", errors="ignore")
    pairs = dict(KV_REGEX.findall(raw))
    if pairs:
        side = (pairs.get("side") or "").lower()
        price = pairs.get("price")
        try:
            price = float(price)
        except (TypeError, ValueError):
            price = None
        if side in ("buy", "sell") and price is not None:
            return side, price

    return None, None

# =========================
# ====  EXCHANGE HELP  ====
# =========================


def get_btcusdt_last_price():
    """Returns latest BTC/USDT last price as float, or None on error."""
    try:
        url = "https://api.coindcx.com/exchange/ticker"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        for item in data:
            mkt = str(item.get("market", "")).upper()
            if "BTCUSDT" in mkt.replace("-", "").replace("_", ""):
                return float(item.get("last_price"))
    except Exception as e:
        print(f"[monitor] price fetch error: {e}")
    return None


def equity_return_pct(position, current_price):
    """
    Returns unrealized equity return as a fraction (0.015 = 1.5%),
    i.e., price_return_pct * leverage. Handles side sign.
    """
    entry = float(position["entry_price"])
    px = float(current_price)
    lev = float(position.get("leverage", LEVERAGE))
    if entry <= 0:
        return 0.0
    if position["side"] == "buy":
        price_ret = (px - entry) / entry
    else:
        price_ret = (entry - px) / entry
    return price_ret * lev

# =========================
# == Dynamic K utilities ===
# =========================


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _lerp(a, b, t):
    return a + (b - a) * _clamp(t, 0.0, 1.0)


def _dynamic_k(pos, atr, watermark):
    """
    Combines profit-based tightening and volatility-based widening.
    Uses watermark as the 'current' price proxy (best seen so far).
    Returns a K to use instead of fixed K_ATR.
    """
    entry = float(pos["entry_price"])
    side = pos["side"]
    px = float(watermark) if watermark else entry

    # 1) Profit-based tightening (equity return already includes leverage)
    er = equity_return_pct({"entry_price": entry, "side": side, "leverage": pos.get("leverage", LEVERAGE)}, px)
    # Map equity return to [0,1] over [ER_TIGHTEN_START, ER_TIGHTEN_END]
    if ER_TIGHTEN_END > ER_TIGHTEN_START:
        t_pnl = (er - ER_TIGHTEN_START) / (ER_TIGHTEN_END - ER_TIGHTEN_START)
    else:
        t_pnl = 0.0
    t_pnl = _clamp(t_pnl, 0.0, 1.0)

    # Base K: start wide, get tighter as PnL improves
    k_pnl = _lerp(K_MAX, K_MIN, t_pnl)

    # 2) Volatility regime adjuster — normalize ATR to price
    base_px = px if px > 0 else entry
    atr_pct = (atr / base_px) if base_px > 0 else 0.0
    if VOL_HIGH > 0 and VOL_HIGH > VOL_LOW:
        t_vol = (atr_pct - VOL_LOW) / (VOL_HIGH - VOL_LOW)
    else:
        t_vol = 0.0
    t_vol = _clamp(t_vol, 0.0, 1.0)

    vol_scale = _lerp(VOL_SCALE_MIN, VOL_SCALE_MAX, t_vol)

    # Combine and clamp
    k = k_pnl * vol_scale
    return _clamp(k, K_MIN, K_MAX)


# Optional: simple “stair-step” alternative (unused by default)
# def _k_stair_step(pos, atr, watermark):
#     entry = float(pos["entry_price"])
#     px    = float(watermark) if watermark else entry
#     move = (px - entry) if pos["side"] == "buy" else (entry - px)
#     R = move / max(atr, 1e-9)
#     if R < 0.5: return 3.0
#     elif R < 1.0: return 2.4
#     elif R < 2.0: return 1.8
#     else: return 1.3

# =========================
# ====  TRADE ACTIONS  ====
# =========================


def open_position(state, side, price):
    """Open a new position via CoinDCX API (market). Falls back gracefully if API data missing."""
    margin = (price * 0.002) / LEVERAGE
    margin = margin * 88.3  # your conversion factor

    timestamp = int(round(time.time() * 1000))

    body = {
        "timestamp": timestamp,
        "order": {
            "side": side,  # "buy" or "sell"
            "pair": "B-BTC_USDT",
            "order_type": "market_order",
            "price": None,
            "stop_price": None,
            "total_quantity": 0.002,
            "leverage": LEVERAGE,
            "notification": "email_notification",
            "hidden": False,
            "post_only": False,
            "margin_currency_short_name": "INR",
        }
    }

    json_body = json.dumps(body, separators=(",", ":"))
    signature = hmac.new(SECRET_BYTES, json_body.encode(), hashlib.sha256).hexdigest()

    url = "https://api.coindcx.com/exchange/v1/derivatives/futures/orders/create"
    headers = {
        'Content-Type': 'application/json',
        'X-AUTH-APIKEY': API_KEY,
        'X-AUTH-SIGNATURE': signature
    }

    entry_price = float(price)
    try:
        resp = requests.post(url, data=json_body, headers=headers, timeout=15)
        data = resp.json()
        # TODO: adjust to real API; fallback keeps entry_price as provided
        print("[open_position] response:", data)
    except Exception as e:
        print(f"[open_position] API error: {e}")

    position = {
        "symbol": "B-BTC_USDT",
        "side": side,                 # "buy" (long) or "sell" (short)
        "entry_price": entry_price,   # best available
        "qty_btc": 0.002,
        "margin_inr": margin,
        "leverage": LEVERAGE,
        "opened_at": timestamp
    }
    state["position"] = position
    return position, None


def close_position(state, price):
    """Close the currently open position via CoinDCX. Computes PnL locally and clears trail for this position."""
    pos = state["position"]
    if not pos:
        return 0.0

    # Try to close on exchange (best effort)
    try:
        timestamp = int(round(time.time() * 1000))
        body = {
            "timestamp": timestamp,
            "page": "1",
            "size": "10",
            "pairs": "B-BTC_USDT",
            "margin_currency_short_name": ["INR"]
        }
        json_body = json.dumps(body, separators=(",", ":"))
        signature = hmac.new(SECRET_BYTES, json_body.encode(), hashlib.sha256).hexdigest()
        url = "https://api.coindcx.com/exchange/v1/derivatives/futures/positions"
        headers = {
            'Content-Type': 'application/json',
            'X-AUTH-APIKEY': API_KEY,
            'X-AUTH-SIGNATURE': signature
        }
        resp = requests.post(url, data=json_body, headers=headers, timeout=15)
        positions = resp.json()
        position_id = None
        if isinstance(positions, list) and positions:
            position_id = positions[0].get("id")  # ideally match by symbol/side

        if position_id:
            body = {"timestamp": int(round(time.time() * 1000)), "id": position_id}
            json_body = json.dumps(body, separators=(",", ":"))
            signature = hmac.new(SECRET_BYTES, json_body.encode(), hashlib.sha256).hexdigest()
            url = "https://api.coindcx.com/exchange/v1/derivatives/futures/positions/exit"
            headers = {
                'Content-Type': 'application/json',
                'X-AUTH-APIKEY': API_KEY,
                'X-AUTH-SIGNATURE': signature
            }
            resp2 = requests.post(url, data=json_body, headers=headers, timeout=15)
            print("[close_position] exit resp:", resp2.text)
    except Exception as e:
        print(f"[close_position] API error: {e}")

    # Local PnL calc (convert to INR inline)
    entry = float(pos["entry_price"])
    qty = pos["qty_btc"]
    side = pos["side"]
    px = float(price)

    if side == "buy":
        pnl_inr = qty * (px - entry) * 88.3
    else:
        pnl_inr = qty * (entry - px) * 88.3

    global INITIAL_BALANCE_INR
    state["balance_inr"] = float(INITIAL_BALANCE_INR) + pnl_inr
    INITIAL_BALANCE_INR = state["balance_inr"]
    state["position"] = None
    if state["balance_inr"] <= 0:
        state["stopped"] = True

    # Clear this position's trail only (avoid name clash with API_KEY)
    trail_key = _pos_key({"position": pos})
    with TRAIL_LOCK:
        trail = _load_trail()
        if trail_key in trail:
            del trail[trail_key]
            _save_trail(trail)

    return pnl_inr

# =========================
# ========  ROUTES  =======
# =========================


@app.route("/status", methods=["GET"])
def status():
    return jsonify(load_state()), 200


@app.route("/reset", methods=["POST"])
def reset():
    # keep INITIAL_BALANCE_INR as-is; only clear position & trail
    state = {"balance_inr": INITIAL_BALANCE_INR, "position": None, "stopped": False}
    save_state(state)
    with TRAIL_LOCK:
        _save_trail({})
    return jsonify({"ok": True, "message": "State reset.", "state": state}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    side, price = parse_alert(request)
    print("[webhook] parsed:", side, price)
    if side is None or price is None:
        return jsonify({"ok": False, "error": "Invalid payload. Expect JSON/form or text: side=buy|sell, price=<number>."}), 400

    state = load_state()

    # 1) close old position if any
    close_pnl = None
    if state["position"] is not None:
        prev = state["position"].copy()
        pnl = close_position(state, price)
        close_pnl = pnl
        log_trade(
            action="close", side=prev["side"], price=price, qty_btc=prev["qty_btc"],
            entry_price=prev["entry_price"], exit_price=price, pnl_inr=pnl,
            margin=prev["margin_inr"], leverage=prev["leverage"], balance_after=state["balance_inr"]
        )
        save_state(state)

    # 2) stop if account halted
    if state.get("stopped", False):
        save_state(state)
        return jsonify({
            "ok": True,
            "message": "Account stopped (balance ≤ 0). Closed previous (if any), not opening a new one.",
            "close_pnl_inr": close_pnl,
            "state": state
        }), 200

    time.sleep(4)  # small grace

    # 3) open new position
    new_pos, err = open_position(state, side, price)
    if err:
        save_state(state)
        return jsonify({"ok": False, "error": err, "state": state}), 400

    log_trade(
        action="open", side=new_pos["side"], price=price, qty_btc=new_pos["qty_btc"],
        entry_price=new_pos["entry_price"], exit_price="", pnl_inr="",
        margin=new_pos["margin_inr"], leverage=new_pos["leverage"], balance_after=state["balance_inr"]
    )
    save_state(state)

    # Clear any previous trail; let /price or /atr initialize for the new position.
    with TRAIL_LOCK:
        _save_trail({})

    return jsonify({
        "ok": True,
        "message": "Closed previous (if any) and opened new position.",
        "close_pnl_inr": close_pnl,
        "opened_position": new_pos,
        "state": state
    }), 200


@app.route("/price", methods=["POST"])
def post_price():
    """Update watermark for the open position. Expect JSON: {'price': float}. Raw numeric fallback is supported."""
    state = load_state()
    pos = state.get("position")
    if not pos:
        return jsonify({"ok": False, "error": "No position."}), 400

    payload = request.get_json(silent=True) or {}
    price = payload.get("price")
    if price is None:
        # fallback: raw text numeric
        try:
            price = float(request.data.decode("utf-8", errors="ignore"))
        except Exception:
            return jsonify({"ok": False, "error": "Send JSON: {'price': float} or raw float"}), 400
    try:
        price = float(price)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid price."}), 400

    key = _pos_key(state)
    with TRAIL_LOCK:
        trail = _load_trail()
        t = trail.get(key)

        if not t:
            # first tick for this position
            watermark = price
            # stop is None until ATR arrives (prevents accidental immediate close)
            t = {"side": pos["side"], "watermark": watermark, "stop": None}
        else:
            watermark = float(t["watermark"])
            if pos["side"] == "buy":
                watermark = max(watermark, price)
            else:
                watermark = min(watermark, price)
            t["watermark"] = watermark

        trail[key] = t
        _save_trail(trail)

    # Optional debug artifact:
    with open('price_tradingview.txt', 'w') as f:
        f.write(str(price))

    return jsonify({"ok": True, "message": "Price updated.", "watermark": t["watermark"], "stop": t["stop"]}), 200


@app.route("/atr", methods=["POST"])
def post_atr(loop_sleep=60, target_equity_return_pct=0.015):
    """Tighten trailing stop using current watermark. Expect JSON: {'atr': float}. Raw numeric fallback is supported."""
    state = load_state()
    pos = state.get("position")
    if not pos:
        return jsonify({"ok": False, "error": "No position."}), 400

    payload = request.get_json(silent=True) or {}
    atr = payload.get("atr")
    if atr is None:
        # fallback: raw text numeric
        try:
            atr = float(request.data.decode("utf-8", errors="ignore"))
        except Exception:
            return jsonify({"ok": False, "error": "Send JSON: {'atr': float} or raw float"}), 400
    try:
        atr = float(atr)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid atr."}), 400

    key = _pos_key(state)
    with TRAIL_LOCK:
        trail = _load_trail()
        t = trail.get(key)

        if not t:
            # initialize using entry as watermark if price not yet posted
            entry = float(pos["entry_price"])
            k_use = _dynamic_k(pos, atr, entry)  # On first ATR, use entry as watermark
            stop = entry - k_use * atr if pos["side"] == "buy" else entry + k_use * atr
            t = {"side": pos["side"], "watermark": entry, "stop": stop, "k": k_use}
        else:
            watermark = float(t["watermark"])
            prev_stop = t.get("stop")  # might be None
            k_use = _dynamic_k(pos, atr, watermark)

            if prev_stop is None:
                # First ATR seen after watermark exists: set initial stop
                stop = (watermark - k_use * atr) if pos["side"] == "buy" else (watermark + k_use * atr)
            else:
                prev_stop = float(prev_stop)
                if pos["side"] == "buy":
                    stop = max(prev_stop, watermark - k_use * atr)
                else:
                    stop = min(prev_stop, watermark + k_use * atr)
            t["stop"] = stop
            t["k"] = k_use

        trail[key] = t
        _save_trail(trail)

        # Optional: legacy compatibility file if anything else reads it
        with open('trail_stp.txt', 'w') as f:
            f.write(str(t["stop"]))

    return jsonify({"ok": True, "message": "Trailing stop updated.", "stop": t["stop"], "watermark": t["watermark"], "k": t.get("k")}), 200

# =========================
# ==  TRAIL MONITOR LOOP ==
# =========================


def monitor_trail_stop():
    """Background thread to close positions when price crosses the trailing stop."""
    while True:
        try:
            state = load_state()
            pos = state.get("position")
            if pos:
                key = _pos_key(state)
                with TRAIL_LOCK:
                    trail = _load_trail()
                    t = trail.get(key)

                if t:
                    stop = t.get("stop")
                    if stop is None:
                        time.sleep(1)
                        continue  # don't close until an ATR-defined stop exists
                    stop = float(stop)

                    # read last observed price (written by /price)
                    px = None
                    try:
                        with open('price_tradingview.txt', 'r') as f:
                            px = float(f.read())
                    except Exception:
                        pass

                    if px is not None:
                        if pos["side"] == "buy" and px <= stop and px>pos["entry_price"]:
                            prev = pos.copy()
                            pnl = close_position(state, px)
                            log_trade(
                                action="close", side=prev["side"], price=px, qty_btc=prev["qty_btc"],
                                entry_price=prev["entry_price"], exit_price=px, pnl_inr=pnl,
                                margin=prev["margin_inr"], leverage=prev["leverage"], balance_after=state["balance_inr"]
                            )
                            save_state(state)
                            with TRAIL_LOCK:
                                trail = _load_trail()
                                if key in trail:
                                    del trail[key]
                                _save_trail(trail)

                        elif pos["side"] == "sell" and px >= stop and px<pos["entry_price"]:
                            prev = pos.copy()
                            pnl = close_position(state, px)
                            log_trade(
                                action="close", side=prev["side"], price=px, qty_btc=prev["qty_btc"],
                                entry_price=prev["entry_price"], exit_price=px, pnl_inr=pnl,
                                margin=prev["margin_inr"], leverage=prev["leverage"], balance_after=state["balance_inr"]
                            )
                            save_state(state)
                            with TRAIL_LOCK:
                                trail = _load_trail()
                                if key in trail:
                                    del trail[key]
                                _save_trail(trail)

            time.sleep(1)  # poll cadence
        except Exception as e:
            print(f"[monitor] error: {e}")
            time.sleep(2)

# =========================
# ========= MAIN ==========
# =========================

if __name__ == "__main__":
    threading.Thread(target=monitor_trail_stop, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)
