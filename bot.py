#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZONE ALERT BOT
==============
Signal 1 (1H):  liquidity swept by WICK, then MSS by BODY close  ->  chart + caption
Signal 2 (5m):  first touch of an unused zone                    ->  reply to Signal 1

Usage
  python bot.py run     normal run (the scheduler calls this)
  python bot.py test    sends a test report + sample chart to Telegram, changes nothing
"""
import io
import json
import math
import os
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None
try:
    import requests as _rq
except Exception:  # pragma: no cover
    _rq = None
try:
    from curl_cffi import requests as _cr
except Exception:  # pragma: no cover
    _cr = None

# =====================================================================
#  SETTINGS  (you normally never touch anything below this block)
# =====================================================================
TIMEZONE = "Asia/Tehran"            # time zone used in captions and on the chart
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

# --- limits that the setup description does not fix (safe defaults) --
SWING_LOOKBACK = 30          # hunted swing may be at most N 1H candles before the sweep candle
MAX_BARS_SWEEP_TO_MSS = 12   # the MSS candle must come within N candles after the sweep candle
ZONE_MAX_AGE_DAYS = 14       # an untouched zone is dropped silently after N days

# --- technical settings ------------------------------------------------
CHART_BARS = 72              # candles shown on the chart
MIN_1H_BARS = 60             # minimum history needed
CLOSE_DELAY_SEC = 12         # wait a few seconds after the hour before reading the candle
CLOSED_FALLBACK_SEC = 20 * 60  # last candle without a successor counts as closed after this
MAX_CATCHUP_BARS = 24        # after downtime: look back at most N closed candles
MAX_LATE_ANNOUNCE_SEC = 24 * 3600  # zones found later than this after formation are not announced
LATE_TOUCH_SEC = 10 * 60     # touch alert older than this is marked "late"
MAX_LATE_TOUCH_SEC = 6 * 3600  # touch older than this is marked as used, without an alert
RUN_DEADLINE_SEC = 240       # a run never works longer than this
HEALTH_WARN_AFTER_SEC = 90 * 60  # warn if a symbol had no data for this long
YAHOO_PAUSE_SEC = 0.35       # polite pause between Yahoo requests
FIVE_MIN_WINDOW_SEC = 6 * 3600  # 5m data requested on every run (older touches are caught by the 1H net)

# One block per symbol:  name shown in Telegram | hashtags | decimals | data sources
#   ("yahoo", ticker) | ("kraken", pair) | ("coinbase", product)
#   The first source that returns data is used (and remembered).  To change a feed,
#   just edit the ticker text, e.g. "XAUUSD=X" -> "GC=F".
#   failover=True -> the sources are the same market, the next one is used if one fails.
SYMBOLS = [
    dict(name="NAS100", tags=["NAS100", "NQ"], dec=1,
         src=[("yahoo", "NQ=F"), ("yahoo", "^NDX")]),
    dict(name="XAUUSD", tags=["XAUUSD", "GOLD"], dec=2,
         src=[("yahoo", "XAUUSD=X"), ("yahoo", "GC=F")]),
    dict(name="US30", tags=["US30", "DOW"], dec=1,
         src=[("yahoo", "YM=F"), ("yahoo", "^DJI")]),
    dict(name="SPX500", tags=["SPX500", "SPX"], dec=2,
         src=[("yahoo", "ES=F"), ("yahoo", "^GSPC")]),
    dict(name="GER40", tags=["GER40", "DAX"], dec=1,
         src=[("yahoo", "^GDAXI")]),
    dict(name="EURUSD", tags=["EURUSD"], dec=5,
         src=[("yahoo", "EURUSD=X")]),
    dict(name="USDJPY", tags=["USDJPY"], dec=3,
         src=[("yahoo", "JPY=X")]),
    dict(name="GBPUSD", tags=["GBPUSD"], dec=5,
         src=[("yahoo", "GBPUSD=X")]),
    dict(name="BTCUSD", tags=["BTCUSD", "BTC"], dec=1, failover=True,
         src=[("kraken", "XBTUSD"), ("coinbase", "BTC-USD"), ("yahoo", "BTC-USD")]),
    dict(name="ETHUSD", tags=["ETHUSD", "ETH"], dec=2, failover=True,
         src=[("kraken", "ETHUSD"), ("coinbase", "ETH-USD"), ("yahoo", "ETH-USD")]),
]

# =====================================================================
#  BASIC HELPERS
# =====================================================================


@dataclass(frozen=True)
class Candle:
    t: int      # open time, epoch seconds (UTC)
    o: float
    h: float
    l: float
    c: float


def log(*a):
    print(datetime.now(timezone.utc).strftime("%H:%M:%S"), *a, flush=True)


def tz():
    try:
        if ZoneInfo is not None:
            return ZoneInfo(TIMEZONE)
    except Exception:
        pass
    return timezone(timedelta(hours=3, minutes=30))  # Tehran (no DST)


def city():
    return TIMEZONE.split("/")[-1].replace("_", " ")


def fmt_time(ts):
    return datetime.fromtimestamp(int(ts), tz()).strftime("%d %b %H:%M")


def fp(p, dec):
    return f"{p:,.{dec}f}"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sym_by_name(name):
    for s in SYMBOLS:
        if s["name"] == name:
            return s
    return None


def zone_tag(z):
    dt = datetime.fromtimestamp(z["formed_close"], tz())
    return f"Z{dt:%y%m%d}_{dt:%H%M}_{z['symbol']}"


# =====================================================================
#  DATA  (Yahoo Finance / Kraken / Coinbase  -  all free, no account)
# =====================================================================
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def http_get(url, params=None, headers=None, timeout=20, impersonate=False):
    """Returns (status_code, text). Never raises. status 0 = network error."""
    hdrs = {"User-Agent": UA, "Accept": "application/json,text/plain,*/*"}
    if headers:
        hdrs.update(headers)
    kinds = []
    if impersonate and _cr is not None:
        kinds.append("curl")
    if _rq is not None:
        kinds.append("requests")
    last = (0, "no http client installed")
    for kind in kinds:
        try:
            if kind == "curl":
                r = _cr.get(url, params=params, headers=hdrs, timeout=timeout,
                            impersonate="chrome")
            else:
                r = _rq.get(url, params=params, headers=hdrs, timeout=timeout)
            return r.status_code, r.text
        except Exception as e:  # network error, timeout, unknown profile ...
            last = (0, f"{type(e).__name__}: {e}")
    return last


def normalize(rows):
    """Sort, de-duplicate and sanity-check candles."""
    by_t = {}
    for c in rows:
        try:
            vals = (c.o, c.h, c.l, c.c)
            if not all(math.isfinite(v) and v > 0 for v in vals):
                continue
            h = max(c.h, c.o, c.c)
            l = min(c.l, c.o, c.c)
            by_t[int(c.t)] = Candle(int(c.t), c.o, h, l, c.c)
        except Exception:
            continue
    return [by_t[k] for k in sorted(by_t)]


def parse_yahoo(text):
    """-> (status, candles, msg)   status: ok | empty | error"""
    try:
        js = json.loads(text)
    except Exception:
        return "error", [], "bad json"
    ch = js.get("chart") or {}
    err = ch.get("error")
    if err:
        code = str(err.get("code", ""))
        return ("empty" if "Not Found" in code else "error"), [], str(err)[:120]
    res = (ch.get("result") or [None])[0]
    if not res:
        return "empty", [], "no result"
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    o, h, l, c = (q.get("open") or []), (q.get("high") or []), (q.get("low") or []), (q.get("close") or [])
    rows = []
    for i, t in enumerate(ts):
        try:
            if None in (o[i], h[i], l[i], c[i]):
                continue
            rows.append(Candle(int(t), float(o[i]), float(h[i]), float(l[i]), float(c[i])))
        except (IndexError, TypeError, ValueError):
            continue
    rows = normalize(rows)
    if not rows:
        return "empty", [], "no candles"
    return "ok", rows, ""


def fetch_yahoo(ticker, interval, rng):
    """rng: a range string like '1mo', or an int = how many seconds back from now."""
    path = "/v8/finance/chart/" + quote(ticker, safe="")
    params = {"interval": interval, "includePrePost": "false"}
    if isinstance(rng, int):
        now = int(time.time())
        params["period1"], params["period2"] = now - rng, now + 900
    else:
        params["range"] = rng
    msg = "?"
    for attempt in range(3):
        host = "query1.finance.yahoo.com" if attempt % 2 == 0 else "query2.finance.yahoo.com"
        time.sleep(YAHOO_PAUSE_SEC)
        code, text = http_get("https://" + host + path, params=params, impersonate=True)
        if code in (200, 404):
            st, rows, m = parse_yahoo(text)
            if st != "error" or code == 200:
                return st, rows, m
            msg = f"HTTP {code}"
            break
        msg = f"HTTP {code} {text[:60] if code == 0 else ''}".strip()
        time.sleep(1.5 * (attempt + 1))
    return "error", [], msg


def fetch_kraken(pair, minutes):
    code, text = http_get("https://api.kraken.com/0/OHLC",
                          params={"pair": pair, "interval": minutes})
    if code != 200:
        return "error", [], f"HTTP {code}"
    try:
        js = json.loads(text)
        if js.get("error"):
            return "error", [], str(js["error"])[:100]
        res = js.get("result") or {}
        keys = [k for k in res if k != "last"]
        rows = [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
                for r in res[keys[0]]]
    except Exception as e:
        return "error", [], f"parse {type(e).__name__}"
    rows = normalize(rows)
    return ("ok", rows, "") if rows else ("empty", [], "no candles")


def fetch_coinbase(product, gran):
    code, text = http_get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                          params={"granularity": gran})
    if code != 200:
        return "error", [], f"HTTP {code}"
    try:
        # [time, low, high, open, close, volume]
        rows = [Candle(int(r[0]), float(r[3]), float(r[2]), float(r[1]), float(r[4]))
                for r in json.loads(text)]
    except Exception as e:
        return "error", [], f"parse {type(e).__name__}"
    rows = normalize(rows)
    return ("ok", rows, "") if rows else ("empty", [], "no candles")


def fetch_one(kind, code, tf, rng):
    if kind == "yahoo":
        return fetch_yahoo(code, "1h" if tf == "1H" else "5m",
                           "1mo" if tf == "1H" else int(rng))
    if kind == "kraken":
        return fetch_kraken(code, 60 if tf == "1H" else 5)
    if kind == "coinbase":
        return fetch_coinbase(code, 3600 if tf == "1H" else 300)
    return "error", [], "unknown source"


def get_candles(sym, st, tf, rng=FIVE_MIN_WINDOW_SEC):
    """-> (status, candles, label). Remembers the working source in st['src']."""
    cands = sym["src"]
    if sym.get("failover"):
        order = list(cands)
    else:
        pin = st.get("src")
        pinned = [c for c in cands if c[1] == pin]
        order = pinned if pinned else list(cands)
    last = ("error", [], "no source")
    for kind, code in order:
        status, rows, msg = fetch_one(kind, code, tf, rng)
        if status == "ok":
            if not sym.get("failover") and not st.get("src"):
                st["src"] = code
            return "ok", rows, f"{kind}:{code}"
        last = (status, [], f"{kind}:{code} {msg}".strip())
        if sym.get("failover"):
            continue
        if status == "empty" and not st.get("src"):
            continue          # symbol not served by this ticker -> try the next candidate
        break
    return last


def closed_candles(cs, now):
    """Candles that are certainly finished (a later candle exists, or enough time passed)."""
    out = []
    n = len(cs)
    for i, c in enumerate(cs):
        if i < n - 1 or now >= c.t + 3600 + CLOSED_FALLBACK_SEC:
            out.append(c)
    return out


# =====================================================================
#  SETUP DETECTION  (your rules)
# =====================================================================
def mirror(cs):
    """Flip prices upside down: a bearish pattern becomes a bullish one."""
    return [Candle(c.t, -c.o, -c.l, -c.h, -c.c) for c in cs]


def is_swing_high(cs, i, hi):
    """3-candle swing high at i (confirmed by candle i+1, which must be <= hi)."""
    return i >= 1 and i + 1 <= hi and cs[i].h > cs[i - 1].h and cs[i].h >= cs[i + 1].h


def is_swing_low(cs, i, hi):
    return i >= 1 and i + 1 <= hi and cs[i].l < cs[i - 1].l and cs[i].l <= cs[i + 1].l


def find_bull_setup(cs, m):
    """
    BUY zone completed by candle m (the newest closed candle).

      1. a swing low is swept by a WICK (price trades below it, the candle body stays above it)
      2. the MSS level = the structural swing high of that leg
           (highest swing high after the hunted swing low; if none, the swing high right
            before the hunted swing low)
      3. the FIRST candle that trades above the MSS level must close ABOVE it with its body
         (wick first, body later = invalid)

    Only candles cs[0..m] are used. Returns a dict or None.
    """
    M = cs[m]
    for s in range(m, max(2, m - MAX_BARS_SWEEP_TO_MSS) - 1, -1):
        S = cs[s]
        pool = []
        for a in range(max(1, s - SWING_LOOKBACK), s):
            if not is_swing_low(cs, a, s):
                continue
            lvl = cs[a].l
            if not S.l < lvl:                       # S must trade below the swing low
                continue
            if min(S.o, S.c) < lvl:                 # ...but its body must stay above it
                continue
            if any(cs[k].l < lvl for k in range(a + 1, s)):
                continue                            # S must be the FIRST candle to take it
            pool.append(a)
        if not pool:
            continue
        a0 = pool[0]                                # oldest swept swing low
        hunt_a = min(pool, key=lambda k: (cs[k].l, k))
        hunt_level = cs[hunt_a].l                   # deepest swept swing low

        highs = [j for j in range(a0 + 1, s) if is_swing_high(cs, j, s)]
        if highs:
            j = max(highs, key=lambda k: (cs[k].h, -k))
        else:
            j = None
            for k in range(a0 - 1, 0, -1):
                if is_swing_high(cs, k, s):
                    j = k
                    break
            if j is None:
                continue
        L = cs[j].h                                 # MSS level
        if any(cs[k].h > L for k in range(j + 1, s)):
            continue                                # it was already touched before the sweep
        if not L > hunt_level:
            continue

        first = next((k for k in range(s, m + 1) if cs[k].h > L), None)
        if first != m:                              # not the first touch of the MSS level
            continue
        if not M.c > L:                             # first touch by wick only -> invalid
            continue
        if any(cs[k].c < hunt_level for k in range(s + 1, m + 1)):
            continue                                # price closed back below the hunted low
        return dict(dir="buy", m=m, s=s, a=hunt_a, j=j, level=L, hunt_level=hunt_level,
                    top=L, bottom=hunt_level, sweep_extreme=S.l)
    return None


def find_setup(cs, m, direction):
    """direction 'buy' or 'sell' (sell = mirror image of buy)."""
    if direction == "buy":
        return find_bull_setup(cs, m)
    r = find_bull_setup(mirror(cs[:m + 1]), m)
    if not r:
        return None
    r = dict(r)
    r["dir"] = "sell"
    r["level"] = -r["level"]
    r["hunt_level"] = -r["hunt_level"]
    r["top"], r["bottom"] = -r["bottom"], -r["top"]
    r["sweep_extreme"] = -r["sweep_extreme"]
    return r


def touched(z, c):
    """Did candle c trade through the alert edge of zone z?"""
    return c.l <= z["level"] if z["dir"] == "buy" else c.h >= z["level"]


def make_zone(sym, setup, cs, src):
    m = setup["m"]
    M = cs[m]
    close_t = M.t + 3600
    return {
        "id": f"{sym['name']}-{'B' if setup['dir'] == 'buy' else 'S'}-{close_t}",
        "symbol": sym["name"], "dir": setup["dir"], "tf": "1H",
        "formed_close": close_t, "close_price": M.c,
        "level": setup["level"], "hunt_level": setup["hunt_level"],
        "top": setup["top"], "bottom": setup["bottom"],
        "sweep_t": cs[setup["s"]].t, "sweep_extreme": setup["sweep_extreme"],
        "hunt_t": cs[setup["a"]].t, "mss_swing_t": cs[setup["j"]].t,
        "src": src, "notified": False, "msg_id": None, "late": False,
        "touched": False, "touch_t": None, "expired": False,
        "win": [[c.t, c.o, c.h, c.l, c.c] for c in cs[max(0, m - CHART_BARS + 1): m + 1]],
    }


# =====================================================================
#  CHART  (Pillow)
# =====================================================================
_FONTS = {}


def _font(size, bold=False):
    from PIL import ImageFont
    key = (size, bold)
    if key in _FONTS:
        return _FONTS[key]
    names = (["DejaVuSans-Bold.ttf"] if bold else []) + ["DejaVuSans.ttf"]
    dirs = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/",
            "/usr/share/fonts/TTF/", "", "/Library/Fonts/", "C:/Windows/Fonts/"]
    f = None
    for nm in names + ["Arial.ttf", "arial.ttf"]:
        for d in dirs:
            try:
                f = ImageFont.truetype(d + nm, size)
                break
            except Exception:
                continue
        if f:
            break
    if f is None:
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
    _FONTS[key] = f
    return f


def _text(d, xy, s, font, fill, anchor="la"):
    try:
        d.text(xy, s, font=font, fill=fill, anchor=anchor)
    except Exception:
        d.text(xy, s, font=font, fill=fill)


def _nice_step(span, target=5):
    raw = span / target
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def render_chart(z, sym):
    """Returns PNG bytes: clean candles + the zone (two horizontal lines + light fill)."""
    from PIL import Image, ImageDraw
    SS, W, H = 2, 1280, 720
    BG, GRID, TXT, DIM = (13, 17, 23), (28, 34, 44), (205, 212, 222), (118, 127, 140)
    UP, DN, AMB = (38, 166, 154), (239, 83, 80), (255, 193, 7)
    dec = sym["dec"]
    buy = z["dir"] == "buy"
    cs = [Candle(int(r[0]), r[1], r[2], r[3], r[4]) for r in z["win"]]
    n = len(cs)

    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img, "RGBA")
    Lx, Rx, Ty, By = 26 * SS, (W - 162) * SS, 86 * SS, (H - 56) * SS
    pad_slots = 12
    sw = (Rx - Lx) / (n + pad_slots)

    def X(i):
        return Lx + (i + 0.5) * sw

    lo = min(min(c.l for c in cs), z["bottom"])
    hi = max(max(c.h for c in cs), z["top"])
    span = (hi - lo) or abs(hi) * 0.001 or 1.0
    lo -= span * 0.07
    hi += span * 0.07

    def Y(p):
        return By - (p - lo) / (hi - lo) * (By - Ty)

    # grid + price labels
    step = _nice_step(hi - lo)
    p = math.ceil(lo / step) * step
    f_small = _font(15 * SS)
    while p < hi:
        y = Y(p)
        d.line([(Lx, y), (Rx, y)], fill=GRID, width=SS)
        _text(d, (Rx + 8 * SS, y), fp(p, dec if step < 1 else max(0, dec - 1)), f_small, DIM, "lm")
        p += step
    # time labels
    every = max(1, n // 6)
    for i in range(every, n, every):
        lbl = datetime.fromtimestamp(cs[i].t, tz()).strftime("%d %b %H:%M")
        try:
            half = d.textlength(lbl, font=f_small) / 2
        except Exception:
            half = 40 * SS
        _text(d, (max(X(i), Lx + half), By + 12 * SS), lbl, f_small, DIM, "ma")

    # zone fill + lines (drawn under the candles)
    idx = {c.t: i for i, c in enumerate(cs)}
    starts = [idx[t] for t in (z["hunt_t"], z["mss_swing_t"]) if t in idx]
    i0 = min(starts) if starts else 0
    zx0 = X(i0) - sw * 0.5
    d.rectangle([zx0, Y(z["top"]), Rx, Y(z["bottom"])], fill=AMB + (34,))

    # candles
    bw = max(2 * SS, sw * 0.62)
    for i, c in enumerate(cs):
        col = UP if c.c >= c.o else DN
        x = X(i)
        d.line([(x, Y(c.h)), (x, Y(c.l))], fill=col, width=max(SS, int(1.6 * SS)))
        yt, yb = Y(max(c.o, c.c)), Y(min(c.o, c.c))
        if yb - yt < 1.5 * SS:
            yt, yb = yt - 0.75 * SS, yb + 0.75 * SS
        d.rectangle([x - bw / 2, yt, x + bw / 2, yb], fill=col)

    # zone lines: MSS level (alert edge) solid, hunt level dashed
    def hline(price, width, dashed):
        y = Y(price)
        if not dashed:
            d.line([(zx0, y), (Rx, y)], fill=AMB, width=width)
            return
        x = zx0
        while x < Rx:
            d.line([(x, y), (min(x + 14 * SS, Rx), y)], fill=AMB + (200,), width=width)
            x += 22 * SS

    hline(z["level"], int(3 * SS), False)
    hline(z["hunt_level"], int(2 * SS), True)

    # markers: sweep wick tip and MSS candle
    f_tag = _font(15 * SS, True)
    si = idx.get(z["sweep_t"])
    if si is not None:
        x, y = X(si), Y(z["sweep_extreme"])
        d.ellipse([x - 5 * SS, y - 5 * SS, x + 5 * SS, y + 5 * SS], outline=AMB, width=2 * SS)
        _text(d, (x, y + (12 if buy else -12) * SS), "SWEEP", f_tag, AMB, "ma" if buy else "ms")
    mi = n - 1
    mc = cs[mi]
    x = X(mi)
    ty = Y(mc.l) + 12 * SS if not buy else Y(mc.h) - 12 * SS
    _text(d, (x, ty), "MSS", f_tag, AMB, "ma" if not buy else "ms")

    # right-side price pills
    def pill(price, label, filled):
        y = Y(price)
        f = _font(15 * SS, True)
        txt = f"{label} {fp(price, dec)}"
        x0, x1 = Rx + 6 * SS, (W - 6) * SS
        d.rounded_rectangle([x0, y - 13 * SS, x1, y + 13 * SS], radius=5 * SS,
                            fill=AMB if filled else (45, 38, 8),
                            outline=AMB, width=SS)
        _text(d, ((x0 + x1) / 2, y), txt, f, (20, 20, 20) if filled else AMB, "mm")

    pill(z["level"], "5m", True)
    pill(z["hunt_level"], "HUNT", False)

    # title / subtitle / footer
    f_title, f_sub = _font(30 * SS, True), _font(17 * SS)
    dot = UP if buy else DN
    d.rounded_rectangle([Lx, 24 * SS, Lx + 12 * SS, 48 * SS], radius=3 * SS, fill=dot)
    _text(d, (Lx + 24 * SS, 36 * SS),
          f"{sym['name']}  ·  1H  ·  {'BUY' if buy else 'SELL'} ZONE", f_title, TXT, "lm")
    _text(d, (Lx + 24 * SS, 64 * SS),
          f"MSS candle closed {fmt_time(z['formed_close'])} ({city()})   "
          f"·   alert edge = {'top' if buy else 'bottom'} line", f_sub, DIM, "lm")
    _text(d, (Lx, (H - 18) * SS), f"data: {z.get('src', '')}", _font(14 * SS), DIM, "lm")

    img = img.resize((W, H), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# =====================================================================
#  TELEGRAM
# =====================================================================
class TelegramError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat = token, chat_id

    def _call(self, method, data=None, files=None):
        if _rq is None:
            raise TelegramError("requests not installed")
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        last = "?"
        for attempt in range(4):
            try:
                r = _rq.post(url, data=data, files=files, timeout=40)
            except Exception as e:
                last = f"{type(e).__name__}"
                time.sleep(2 * (attempt + 1))
                continue
            try:
                js = r.json()
            except Exception:
                js = {}
            if r.status_code == 200 and js.get("ok"):
                return js["result"]
            last = f"HTTP {r.status_code} {str(js.get('description', ''))[:120]}"
            if r.status_code == 429:
                wait = (js.get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(int(wait) + 1, 30))
                continue
            if r.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            break
        raise TelegramError(last)

    @staticmethod
    def _reply(reply_to):
        if not reply_to:
            return {}
        return {"reply_parameters": json.dumps({"message_id": int(reply_to),
                                                "allow_sending_without_reply": True})}

    def send_message(self, text, reply_to=None):
        data = {"chat_id": self.chat, "text": text, "parse_mode": "HTML",
                "disable_web_page_preview": "true"}
        data.update(self._reply(reply_to))
        return self._call("sendMessage", data=data)["message_id"]

    def send_photo(self, png, caption, reply_to=None):
        data = {"chat_id": self.chat, "caption": caption, "parse_mode": "HTML"}
        data.update(self._reply(reply_to))
        files = {"photo": ("zone.png", png, "image/png")}
        return self._call("sendPhoto", data=data, files=files)["message_id"]


# =====================================================================
#  MESSAGES
# =====================================================================
def formation_caption(z, sym):
    dec, buy = sym["dec"], z["dir"] == "buy"
    tags = " ".join("#" + t for t in sym["tags"])
    head = "🟢 <b>BUY ZONE</b>" if buy else "🔴 <b>SELL ZONE</b>"
    lines = [
        f"{head}   {tags}",
        f"⏱ 1H closed: {fmt_time(z['formed_close'])} ({city()})",
        "",
        f"📦 Zone: {fp(z['bottom'], dec)} – {fp(z['top'], dec)}",
        f"🎯 5m alert edge: <b>{fp(z['level'], dec)}</b> ({'top' if buy else 'bottom'} line)",
        f"🪝 Hunt: swing {'low' if buy else 'high'} {fp(z['hunt_level'], dec)} swept by wick",
        f"🧱 MSS: body closed {'above' if buy else 'below'} {fp(z['level'], dec)}",
    ]
    if z.get("late"):
        lines.append("⚠️ detected late (bot was waiting for data)")
    lines += ["", f"🏷 #{zone_tag(z)}"]
    return "\n".join(lines)


def touch_text(z, sym, bar_t, now, price=None):
    dec, buy = sym["dec"], z["dir"] == "buy"
    tags = " ".join("#" + t for t in sym["tags"])
    lines = [
        f"⚡ <b>PRICE ENTERING ZONE</b>   {tags}",
        f"{'🟢 BUY' if buy else '🔴 SELL'} zone (1H {fmt_time(z['formed_close'])}) – "
        f"first touch of {fp(z['level'], dec)}",
        f"🕒 touch: {fmt_time(bar_t)} ({city()})" + (f"   ·   price {fp(price, dec)}" if price else ""),
    ]
    if now - bar_t > LATE_TOUCH_SEC:
        lines.append(f"⚠️ late alert – detected {int((now - bar_t) / 60)} min after the touch")
    lines += ["👉 go to the chart and wait for your 1m shift", "", f"🏷 #{zone_tag(z)}"]
    return "\n".join(lines)


# =====================================================================
#  STATE
# =====================================================================
def load_state():
    s = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if not isinstance(s, dict):
            s = {}
    except FileNotFoundError:
        s = {}
    except Exception as e:
        log("state file unreadable, starting fresh:", e)
        try:
            os.replace(STATE_FILE, STATE_FILE + ".broken")
        except Exception:
            pass
        s = {}
    s.setdefault("version", 1)
    s.setdefault("symbols", {})
    s.setdefault("zones", [])
    return s


def save_state(s):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# =====================================================================
#  ENGINE
# =====================================================================
class Env:
    """Everything the engine needs from the outside world (swapped by the tests)."""

    def __init__(self, tg, fetch=get_candles, clock=time.time):
        self.tg, self.fetch, self.clock = tg, fetch, clock
        self.checkpoint = lambda: None


def note_failure(env, sym, st, tf, msg, now):
    f = st.setdefault("fail_" + tf, {})
    if not f.get("first"):
        f["first"] = now
    log(f"[{sym['name']}] data problem ({tf}): {msg}")
    if now - f["first"] >= HEALTH_WARN_AFTER_SEC and not f.get("warned"):
        try:
            env.tg.send_message(f"⚠️ <b>Data problem</b> #{sym['name']} ({tf})\n"
                                f"No data for {int((now - f['first']) / 60)} min.\n{esc(msg)[:200]}")
            f["warned"] = True
        except Exception as e:
            log("could not send health warning:", e)


def note_success(env, sym, st, tf, now):
    f = st.get("fail_" + tf)
    if f:
        if f.get("warned"):
            try:
                env.tg.send_message(f"✅ Data OK again  #{sym['name']} ({tf})")
            except Exception as e:
                log("could not send recovery note:", e)
        st.pop("fail_" + tf, None)


def need_1h(st, now):
    last = st.get("last_1h_close")
    if last is None:
        return True
    if now < last + 3600 + CLOSE_DELAY_SEC:
        return False
    return now >= st.get("next_1h_try", 0)     # set only while the market is closed / the feed is late


def active_zones(zones):
    return [z for z in zones if z["notified"] and not z["touched"] and not z["expired"]]


def do_1h(env, sym, st, state, zones, now):
    status, cs, src = env.fetch(sym, st, "1H", None)
    if status != "ok":
        note_failure(env, sym, st, "1H", src, now)
        return
    note_success(env, sym, st, "1H", now)
    closed = closed_candles(cs, now)
    if len(closed) < MIN_1H_BARS:
        return
    newest_close = closed[-1].t + 3600
    last_close = st.get("last_1h_close")
    if last_close is None:                       # first run: look at the newest closed candle only
        last_close = closed[-2].t + 3600 if len(closed) >= 2 else newest_close
    idxs = [i for i, c in enumerate(closed) if c.t + 3600 > last_close][-MAX_CATCHUP_BARS:]
    known = {z["id"] for z in state["zones"]}
    for i in idxs:
        for direction in ("buy", "sell"):
            setup = find_setup(closed, i, direction)
            if not setup:
                continue
            z = make_zone(sym, setup, closed, src)
            if z["id"] in known:
                continue
            known.add(z["id"])
            after = [c for c in closed if c.t >= z["formed_close"]]
            used = next((c for c in after if touched(z, c)), None)
            if used is not None:                 # already used by later candles -> silent
                z.update(touched=True, touch_t=used.t, notified=True, win=[])
            elif now - z["formed_close"] > MAX_LATE_ANNOUNCE_SEC:
                z.update(expired=True, notified=True, win=[])
            elif now - z["formed_close"] > 2 * 3600:
                z["late"] = True
            state["zones"].append(z)
            zones.append(z)
            log(f"[{sym['name']}] new {direction} zone, level {z['level']}")
    # safety net: a touch visible in closed 1H candles that the 5m path did not catch
    for z in active_zones(zones):
        hit = next((c for c in closed if c.t >= z["formed_close"] and touched(z, c)), None)
        if hit is not None:
            z["touched"], z["touch_t"] = True, hit.t
            if now - hit.t <= MAX_LATE_TOUCH_SEC:
                send_touch(env, sym, z, hit.t, now, None)
    st["last_1h_close"] = newest_close
    # how long is the feed behind?  (market closed / feed late -> look less often, without noise)
    behind = now - (newest_close + 3600)
    if behind < 20 * 60:
        st.pop("next_1h_try", None)            # normal: retry on every run until the candle exists
    elif now - newest_close > 3 * 3600:        # market closed: look again right after the next hour
        st["next_1h_try"] = (now // 3600 + 1) * 3600 + CLOSE_DELAY_SEC
    else:                                      # feed is late: look every 20 minutes
        st["next_1h_try"] = now + 20 * 60


def send_touch(env, sym, z, bar_t, now, price):
    try:
        env.tg.send_message(touch_text(z, sym, bar_t, now, price), reply_to=z.get("msg_id"))
        z["touch_sent"] = True
        env.checkpoint()
    except Exception as e:
        log(f"[{sym['name']}] touch alert not sent: {e}")
        z["touched"] = False            # try again next run
        z["touch_t"] = None


def announce_zones(env, sym, zones, now):
    for z in sorted([z for z in zones if not z["notified"]], key=lambda q: q["formed_close"]):
        try:
            png = render_chart(z, sym)
            mid = env.tg.send_photo(png, formation_caption(z, sym))
        except Exception as e:
            log(f"[{sym['name']}] zone not announced yet: {e}")
            continue
        z["notified"], z["msg_id"], z["win"] = True, mid, []
        env.checkpoint()
        log(f"[{sym['name']}] announced {z['id']}")


def do_5m(env, sym, st, zones, now):
    act = active_zones(zones)
    if not act or now < st.get("next_5m_try", 0):
        return
    status, cs, src = env.fetch(sym, st, "5m", FIVE_MIN_WINDOW_SEC)
    if status != "ok":
        note_failure(env, sym, st, "5m", src, now)
        return
    note_success(env, sym, st, "5m", now)
    price = cs[-1].c
    lag = now - cs[-1].t
    if lag > 2 * 3600:                           # no fresh bars: market closed -> look every 10 minutes
        st["next_5m_try"] = now + 600
    else:
        st.pop("next_5m_try", None)
    for z in sorted(act, key=lambda q: q["formed_close"]):
        hit = next((c for c in cs if c.t >= z["formed_close"] and touched(z, c)), None)
        if hit is None:
            continue
        z["touched"], z["touch_t"] = True, hit.t
        if now - hit.t > MAX_LATE_TOUCH_SEC:
            continue                    # too old to be useful: mark as used silently
        send_touch(env, sym, z, hit.t, now, price if lag < 600 else None)


def step_symbol(env, sym, state, now):
    st = state["symbols"].setdefault(sym["name"], {})
    zones = [z for z in state["zones"] if z["symbol"] == sym["name"]]
    if need_1h(st, now):
        do_1h(env, sym, st, state, zones, now)
    announce_zones(env, sym, zones, now)
    for z in zones:                                        # expire old untouched zones
        if not z["touched"] and not z["expired"] and now - z["formed_close"] > ZONE_MAX_AGE_DAYS * 86400:
            z["expired"] = True
    do_5m(env, sym, st, zones, now)


def prune(state, now):
    keep = []
    for z in state["zones"]:
        done = z["touched"] or z["expired"]
        if done and now - z["formed_close"] > 5 * 86400:
            continue
        keep.append(z)
    state["zones"] = keep[-300:]


def run_once(env, symbols=None):
    t0 = time.time()
    now = int(env.clock())
    state = load_state()
    env.checkpoint = lambda: save_state(state)
    if not state.get("started"):
        try:
            env.tg.send_message("✅ <b>Zone Bot is running</b>\n"
                                + ", ".join(s["name"] for s in (symbols or SYMBOLS))
                                + "\n1️⃣ 1H zone formed → chart + caption\n"
                                  "2️⃣ price enters an unused zone (5m) → reply under that chart")
            state["started"] = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
        except Exception as e:
            log("start message not sent:", e)
    for sym in (symbols or SYMBOLS):
        if time.time() - t0 > RUN_DEADLINE_SEC:
            log("deadline reached, remaining symbols next run")
            break
        try:
            step_symbol(env, sym, state, now)
        except Exception:
            log(f"[{sym['name']}] unexpected error:\n{traceback.format_exc()}")
    prune(state, now)
    state["heartbeat"] = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d")
    save_state(state)
    n_act = len([z for z in state["zones"] if z["notified"] and not z["touched"] and not z["expired"]])
    log(f"run finished, active zones: {n_act}")


# =====================================================================
#  TEST MODE
# =====================================================================
def _sample_zone(sym, cs, src):
    """Builds a sample zone from the latest candles (only to test the chart + Telegram)."""
    w = cs[-CHART_BARS:]
    m = len(w) - 1
    hi_i = max(range(max(1, m - 25), m), key=lambda i: w[i].h)
    lo_i = min(range(max(1, m - 25), m), key=lambda i: w[i].l)
    level, hunt = w[hi_i].h, w[lo_i].l
    if not level > hunt:
        level, hunt = max(c.h for c in w), min(c.l for c in w)
    return {
        "id": "TEST", "symbol": sym["name"], "dir": "buy", "tf": "1H",
        "formed_close": w[-1].t + 3600, "close_price": w[-1].c,
        "level": level, "hunt_level": hunt, "top": level, "bottom": hunt,
        "sweep_t": w[lo_i].t, "sweep_extreme": hunt, "hunt_t": w[lo_i].t,
        "mss_swing_t": w[hi_i].t, "src": src,
        "win": [[c.t, c.o, c.h, c.l, c.c] for c in w],
    }


def run_test(tg):
    now = int(time.time())
    lines = ["🧪 <b>Zone Bot – TEST</b>", "Telegram: OK ✅", ""]
    sample = None
    for sym in SYMBOLS:
        st = {}
        s1, c1, src = get_candles(sym, st, "1H", None)
        s5, c5, src5 = get_candles(sym, st, "5m", FIVE_MIN_WINDOW_SEC) if s1 == "ok" else ("skip", [], "")
        if s1 == "ok" and s5 == "ok":
            lag = int((now - c5[-1].t) / 60)
            note = "market closed?" if lag > 180 else (f"feed lag ~{lag} min ⚠️" if lag > 8 else "live ✅")
            if len(c1) < 120:
                note += " · few candles ⚠️"
            lines.append(f"{sym['name']}: {esc(src.split(':', 1)[1])} · 1H {len(c1)} · 5m {len(c5)} · {note}")
            if sample is None and len(closed_candles(c1, now)) >= 40:
                sample = (sym, closed_candles(c1, now), src)
        else:
            lines.append(f"{sym['name']}: ❌ {esc(src)[:80]}")
    tg.send_message("\n".join(lines))
    if sample:
        sym, cs, src = sample
        z = _sample_zone(sym, cs, src)
        cap = ("🧪 <b>TEST chart</b> – this is NOT a signal\n"
               f"{sym['name']} · sample zone from the latest candles")
        mid = tg.send_photo(render_chart(z, sym), cap)
        tg.send_message("🧪 Reply test – if you see this as a reply under the chart, replies work ✅",
                        reply_to=mid)
    else:
        tg.send_message("🧪 No symbol returned enough data for a sample chart.")
    log("test finished")


# =====================================================================
def main(argv):
    mode = argv[1] if len(argv) > 1 else "run"
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set (Settings > Secrets > Actions)")
        return 1
    tg = Telegram(token, chat)
    if mode == "test":
        run_test(tg)
        return 0
    run_once(Env(tg))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
