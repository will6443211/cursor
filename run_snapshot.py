#!/usr/bin/env python3
"""Hourly watchlist: external + index + sector flow + MA/volume screen + buy/no-buy."""
import json, urllib.request, datetime, os, sys, html, re, subprocess, webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))

def load_watchlist():
    return json.load(open(os.path.join(ROOT, "watchlist.json"), encoding="utf-8"))

WL = load_watchlist()
try:
    MACRO = json.load(open(os.path.join(ROOT, "macro_notes.json"), encoding="utf-8"))
except Exception:
    MACRO = {}


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 拉不到数据时静默吞掉，会让报告拿着残缺数据照样给出一副很确定的结论。
# 这里统一做退避重试，并把失败次数记下来在报告里报出去。
FETCH_FAIL = {}


def _note_fail(tag):
    FETCH_FAIL[tag] = FETCH_FAIL.get(tag, 0) + 1


def _get(url, headers, timeout, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout).read()
        except Exception as e:
            last = e
            if i < tries - 1:
                import time as _t
                _t.sleep(0.4 * (2 ** i))
    raise last


def http(url, gbk=False, timeout=12, tries=3):
    b = _get(url, {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}, timeout, tries)
    return b.decode("gbk", "replace") if gbk else json.loads(b)


NAME_CACHE = {}  # code -> name。给 ST 判定用，避免各处再传 name


def _f(x, d=None):
    try:
        if x in ("", "-", None):
            return d
        return float(x)
    except Exception:
        return d


def tencent(codes):
    raw = http("https://qt.gtimg.cn/q=" + ",".join(codes), gbk=True, timeout=15)
    out = {}
    for line in raw.strip().split(";"):
        if '="' not in line:
            continue
        p = line.split('="')[1].split("~")
        if len(p) < 50:
            continue
        try:
            row = {
                "name": p[1],
                "px": float(p[3]),
                "prev": float(p[4]),
                "open": float(p[5]),
                "chg": float(p[32]),
                "high": float(p[33]),
                "low": float(p[34]),
                "amp": float(p[43] or 0),
                "mcap": float(p[45] or 0),
                "turnover": float(p[38] or 0) if len(p) > 38 and p[38] not in ("", "-") else None,
                "vol_ratio": float(p[49] or 0),
                "vwap": float(p[51]) if len(p) > 51 and p[51] not in ("", "-") else None,
                "amt_wan": float(p[36] or 0),
                # 买卖一档：封单额/承接量用，打板质量靠这个，不再靠猜
                "bid1": _f(p[9]), "bid1_lot": _f(p[10], 0),
                "ask1": _f(p[19]), "ask1_lot": _f(p[20], 0),
            }
            out[p[2]] = row
            NAME_CACHE[p[2]] = p[1]
        except Exception:
            continue
    return out


def yahoo_hist(sym):
    data = http(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=6mo")
    res = data["chart"]["result"][0]
    ts, q = res["timestamp"], res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        if None in (o, h, l, c, v) or v == 0:
            continue
        d = datetime.datetime.fromtimestamp(t, datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        bars.append((d, o, h, l, c, v))
    return strip_today(bars)


def strip_today(bars):
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    if bars and bars[-1][0] == today:
        return bars[:-1]
    return bars


def tencent_symbol(s):
    return ("sh" if s.get("market") == "sh" else "sz") + s["code"]


def http_json(url, timeout=12, referer="https://gu.qq.com/", tries=3):
    b = _get(url, {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}, timeout, tries)
    return json.loads(b.decode("utf-8", "replace"))


def tencent_daily(s, n=160):
    """A股前复权日K。东财 kline 盘后常空，腾讯作主源；单域名限频时换备用域名。"""
    code = tencent_symbol(s)
    hosts = ("web.ifzq.gtimg.cn", "proxy.finance.qq.com/ifzqgtimg", "ifzq.gtimg.cn")
    d, rows, data = None, [], {}
    for h in hosts:
        url = f"https://{h}/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
        try:
            d = http_json(url, timeout=10, tries=2)
        except Exception:
            continue
        data = ((d.get("data") or {}).get(code) or {})
        rows = data.get("qfqday") or data.get("day") or []
        if rows:
            break
    if not rows:
        _note_fail("日线")
        return []
    bars = []
    for r in rows:
        if not r or len(r) < 6:
            continue
        dte, o, c, h, l, v = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        if v == 0:
            continue
        bars.append((dte, o, h, l, c, v))
    return bars


def tencent_minute(s):
    """当日分时：time px vol amount。用来算 ORB / 竞价额。"""
    code = tencent_symbol(s)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={code}"
    d = http_json(url, timeout=10)
    rows = (((d.get("data") or {}).get(code) or {}).get("data") or {}).get("data") or []
    pts = []
    for row in rows:
        p = str(row).split()
        if len(p) < 3:
            continue
        hm = p[0]
        if not hm.isdigit():
            continue
        if len(hm) == 3:
            hm = "0" + hm
        if len(hm) == 4:
            hm = hm[:2] + ":" + hm[2:]
        try:
            px, vol = float(p[1]), float(p[2])
        except Exception:
            continue
        amt = None
        if len(p) > 3:
            try:
                amt = float(p[3])
            except Exception:
                amt = None
        pts.append((hm, px, vol, amt))
    return pts


def daily_hist(s):
    try:
        bars = tencent_daily(s)
        if len(bars) >= 30:
            return bars
    except Exception:
        bars = []
    try:
        eb = em_daily(s)
        if len(eb) >= 30:
            return eb
    except Exception:
        pass
    ysym = s["code"] + (".SS" if s.get("market") == "sh" else ".SZ")
    try:
        yb = yahoo_hist(ysym)
        if yb:
            return yb
    except Exception:
        pass
    if not bars:
        _note_fail("日线全部源")
    return bars or []


def em_daily(s, n=160):
    """东财日K备源。腾讯限频时顶上，口径同为前复权。"""
    code = s["code"]
    mkt = 1 if (s.get("market") or infer_market(code)) == "sh" else 0
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get?"
        f"secid={mkt}.{code}&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57"
        f"&klt=101&fqt=1&end=20500101&lmt={n}"
        "&ut=fa5fd1943c7b386f172d6893dbfba10b"
    )
    d = http_json(url, timeout=10, referer="https://quote.eastmoney.com/", tries=2)
    klines = ((d.get("data") or {}) or {}).get("klines") or []
    bars = []
    for ln in klines:
        p = str(ln).split(",")
        if len(p) < 6:
            continue
        try:
            dte, o, c, h, l, v = p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])
        except Exception:
            continue
        if v == 0:
            continue
        bars.append((dte, o, h, l, c, v))
    return bars


def sma(a, n):
    return sum(a[-n:]) / n if len(a) >= n else None


def rsi(closes, n=14):
    """Wilder RSI。表一追高罚和表三超卖用，不改表一权重。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def clip(x, a, b):
    return max(a, min(b, x))


def is_20cm(code):
    return code.startswith(("300", "301", "688"))


def is_st(code, name=None):
    nm = (name or NAME_CACHE.get(code) or "").upper()
    return "ST" in nm or "退" in nm


def board_limit_pct(code, name=None):
    """涨跌停幅度。ST 5%、北交所 30%、创业板/科创 20%、其余 10%。"""
    if is_st(code, name):
        return 5.0
    if code.startswith(("8", "4", "920")):
        return 30.0
    if code.startswith(("300", "301", "688")):
        return 20.0
    return 10.0


def is_limit_up(code, chg, name=None):
    lim = board_limit_pct(code, name)
    return chg >= lim - 0.5


def is_limit_down(code, chg, name=None):
    lim = board_limit_pct(code, name)
    return chg <= -(lim - 0.5)


def overheat(code, chg, name=None):
    """过热分档。按本板涨停幅度折算，10cm 保持原来的 5%/7%/9.2% 手感，20cm 不再被 7% 一刀切。"""
    lim = board_limit_pct(code, name)
    if chg is None:
        return "正常"
    if is_limit_up(code, chg, name):
        return "涨停"
    r = chg / lim if lim else 0
    if r >= 0.92:
        return "见顶"
    if r >= 0.70:
        return "不追"
    if r >= 0.50:
        return "偏热"
    return "正常"


# A股日内成交量 U 型分布：到该时点应完成的当日成交量占比
VOL_CURVE = [
    ("09:30", 0.00), ("10:00", 0.22), ("10:30", 0.35), ("11:00", 0.45),
    ("11:30", 0.54), ("13:00", 0.54), ("13:30", 0.63), ("14:00", 0.72),
    ("14:30", 0.82), ("15:00", 1.00),
]


def session_progress(now=None):
    """(已走时间占比, 应完成成交量占比)。收盘后/盘前都给 (1,1)，阈值不做时段调整。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hm = now.strftime("%H:%M")
    if hm < "09:30":
        return 1.0, 1.0
    if hm >= "15:00":
        return 1.0, 1.0
    if "11:30" <= hm < "13:00":
        return 0.5, 0.54
    mins = int(hm[:2]) * 60 + int(hm[3:])
    elapsed = mins - (9 * 60 + 30) if hm < "11:30" else 120 + (mins - 13 * 60)
    t = clip(elapsed / 240.0, 0.01, 1.0)
    v = 0.0
    for i in range(1, len(VOL_CURVE)):
        t0, v0 = VOL_CURVE[i - 1]
        t1, v1 = VOL_CURVE[i]
        if t0 <= hm < t1:
            span = (int(t1[:2]) * 60 + int(t1[3:])) - (int(t0[:2]) * 60 + int(t0[3:]))
            done = mins - (int(t0[:2]) * 60 + int(t0[3:]))
            v = v0 + (v1 - v0) * (done / span if span else 1)
            break
    else:
        v = 1.0
    return t, clip(v, 0.02, 1.0)


def vr_norm(vr, now=None):
    """量比时段归一。早盘成交前置，1.5 的量比在 10:00 并不等于 14:30 的 1.5。"""
    if not vr:
        return vr or 0
    t, v = session_progress(now)
    if v <= 0:
        return vr
    return vr * (t / v)


def hs_proj(hs, now=None):
    """把当前换手率折算成全天预估换手，5% 的门槛才有一致含义。
    外推倍数封在 4.5 倍（约等于 10:00 的水平）：开盘几分钟的样本太少，
    再往上外推就是拿噪声当结论了。"""
    if hs is None:
        return None
    t, v = session_progress(now)
    if not v:
        return hs
    return hs * min(1.0 / v, 4.5)


def limit_open_dump(s, q):
    """竞价涨停/近板开，开后破开盘或从开盘回落≥3% → 拉高出货，不是抢筹。"""
    if not s or not q:
        return False
    prev, o, p = q.get("prev") or 0, q.get("open") or 0, q.get("px") or 0
    if prev <= 0 or o <= 0 or p <= 0:
        return False
    gap = (o / prev - 1) * 100
    lim = board_limit_pct(s["code"], q.get("name"))
    if not (is_limit_up(s["code"], gap, q.get("name")) or gap >= lim * 0.9):
        return False
    drop = (o - p) / o * 100
    return p < o * 0.997 or drop >= 3.0


def trend_chg_cap(code, atr_pct):
    """趋势今涨上限：跟 ATR 和本板涨停幅度走，不再 3% 一刀切。"""
    board = board_limit_pct(code)
    ap = atr_pct if atr_pct else 2.5
    return min(board * 0.35, max(1.2, ap * 1.2))


def seal_quality(q, code):
    """封单质量。买一价贴涨停时 封单额=买一量×100×价，封成比=封单额/成交额。"""
    out = {"at_limit": False, "seal_yi": None, "seal_ratio": None, "tag": "-"}
    if not q:
        return out
    if not is_limit_up(code, q.get("chg") or 0, q.get("name")):
        return out
    out["at_limit"] = True
    b1, lot = q.get("bid1"), q.get("bid1_lot") or 0
    if not b1 or lot <= 0:
        out["tag"] = "封单未知"
        return out
    seal = b1 * lot * 100
    amt = (q.get("amt_wan") or 0) * 1e4
    out["seal_yi"] = seal / 1e8
    if amt > 0:
        out["seal_ratio"] = seal / amt
    r = out["seal_ratio"]
    if r is None:
        out["tag"] = f"封单{out['seal_yi']:.2f}亿"
    elif r >= 0.5:
        out["tag"] = f"硬板 封单{out['seal_yi']:.2f}亿/封成{r:.2f}"
    elif r >= 0.15:
        out["tag"] = f"一般 封单{out['seal_yi']:.2f}亿/封成{r:.2f}"
    else:
        out["tag"] = f"弱板 封单{out['seal_yi']:.2f}亿/封成{r:.2f}"
    return out


def open_times(pts, prev, code, name=None):
    """开板次数：分时触及涨停价后又离开的次数。烂板和硬板不能同价看待。"""
    if not pts or not prev:
        return None
    lim = limit_price(prev, code, name)
    if not lim:
        return None
    touched, opens, on = False, 0, False
    for row in pts:
        p = row[1]
        at = p >= lim - 0.005
        if at:
            touched, on = True, True
        elif on:
            opens += 1
            on = False
    return opens if touched else None


def vwap_path(q, pts):
    """均价：一直在上 / 曾跌破后站回 / 仍在下。关键低取当日低（均价下探出来的底）。"""
    vwap = q.get("vwap") if q else None
    px = q.get("px") if q else None
    low = q.get("low") if q else None
    out = {
        "vwap_pos": "均价缺", "vwap_reclaim": False, "vwap_held": True, "vwap_key_low": None,
    }
    if vwap is None or px is None:
        return out
    prints = [p for t, p, *_ in (pts or []) if t >= "09:30"]
    min_p = min(prints) if prints else low
    was_below = False
    if low is not None and low < vwap * 0.999:
        was_below = True
    if min_p is not None and min_p < vwap * 0.999:
        was_below = True
    key = None
    if was_below:
        cands = [x for x in (low, min_p) if x is not None]
        key = min(cands) if cands else None
    out["vwap_key_low"] = key
    held = True
    if was_below and key is not None:
        held = px >= key * 1.003
    out["vwap_held"] = held
    if px < vwap:
        out["vwap_pos"] = "均价下"
        out["vwap_reclaim"] = False
        return out
    if was_below:
        out["vwap_pos"] = "站回均价"
        out["vwap_reclaim"] = True
        return out
    out["vwap_pos"] = "均价上"
    out["vwap_reclaim"] = False
    out["vwap_held"] = True
    return out


def yday_limit_down(code, hist):
    done = strip_today(hist) if hist else []
    if not done or len(done) < 2:
        return False
    prev, prev2 = done[-1][4], done[-2][4]
    if prev2 <= 0:
        return False
    chg = (prev / prev2 - 1) * 100
    return is_limit_down(code, chg)


def yday_dt_shape(q, yld):
    """昨跌停次日：trap骗炮 / turn弱转强 / weak续弱。不再一刀切不买。"""
    if not yld or not q or not q.get("prev"):
        return None
    o, p, prev = q["open"], q["px"], q["prev"]
    h = q["high"]
    vwap = q.get("vwap")
    gap = (o / prev - 1) * 100
    chg = q["chg"]
    vr = vr_norm(q.get("vol_ratio") or 0)
    if gap >= 0.5 and p < o:
        return "trap"
    if gap <= -0.15 and h > prev and p < o:
        return "trap"
    # 弱转强要的是低开+站上均价+放量，不是随便飘个红
    if chg > 0 and gap <= -1.0 and p >= o and (vwap is None or p >= vwap) and vr >= 1.2:
        return "turn"
    if chg > 0:
        return "weak"
    return "weak"


def index_shape(q):
    if not q or q["prev"] <= 0:
        return "形态不明"
    o, h, l, p, prev = q["open"], q["high"], q["low"], q["px"], q["prev"]
    gap = (o / prev - 1) * 100
    up = h > prev
    down_from_high = (p / h - 1) * 100 if h else 0
    if gap >= 0.3 and p < o and down_from_high <= -0.4:
        return "高开低走"
    if gap <= -0.3 and up and p < (h + prev) / 2:
        return "低开冲高回落"
    if gap <= -0.3 and p >= prev:
        return "低开翻红"
    if gap >= 0 and p >= o:
        return "高开或平开偏强"
    if p < o:
        return "开后走弱"
    return "震荡"


def score_row(hist, live, code=""):
    """表一正常打分。权重不变。均线用已完成日K，现价比均线；量能用实时量比。"""
    done = strip_today(hist) if hist else []
    if not done:
        done = hist or []
    c = [b[4] for b in done]
    v = [b[5] for b in done]
    live_px = live["px"] if live else None
    px = live_px if live_px else (c[-1] if c else 0)
    ma5, ma10, ma20, ma60 = sma(c, 5), sma(c, 10), sma(c, 20), sma(c, 60)
    v20 = sma(v, 20)
    live_vr = live.get("vol_ratio") if live else None
    hist_vr = (v[-1] / v20) if v20 else None
    vr = live_vr if live_vr is not None else hist_vr
    r20 = px / c[-21] - 1 if len(c) > 21 else None
    hi20 = max(b[2] for b in done[-20:]) if done else px
    if live and live.get("high"):
        hi20 = max(hi20, live["high"])
    dd = px / hi20 - 1 if hi20 else 0
    rs = rsi(c + [px] if live_px else c)
    trend = 0
    for m in (ma5, ma10, ma20, ma60):
        if m and px > m:
            trend += 1
    if ma5 and ma10 and ma5 > ma10:
        trend += 1
    if ma10 and ma20 and ma10 > ma20:
        trend += 1
    if ma20 and ma60 and ma20 > ma60:
        trend += 1
    s_ma = trend / 7 * 100
    s_vol = clip(50 + ((vr or 1) - 1) * 40, 0, 100)
    s_mom = clip(50 + (r20 or 0) * 200, 0, 100)
    chase_pen = 0
    if rs and rs >= 70:
        chase_pen += 20
    if dd > -0.02:
        chase_pen += 10
    live_chg = live["chg"] if live else 0
    oh = overheat(code, live_chg, live.get("name")) if live else "正常"
    if oh in ("涨停", "见顶"):
        chase_pen += 30
    elif oh == "不追":
        chase_pen += 20
    elif oh == "偏热":
        chase_pen += 12
    vs_vwap = 0
    if live and live.get("vwap"):
        vs_vwap = (live["px"] / live["vwap"] - 1) * 100
    buy = 0.30 * s_ma + 0.20 * s_vol + 0.20 * s_mom + 0.15 * (100 if vs_vwap >= 0 else 40) + 0.15 * clip(50 + live_chg * 3, 0, 100) - chase_pen
    return {
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60, "vr": vr, "r20": r20,
        "dd": dd, "rsi": rs, "s_ma": s_ma, "buy": buy, "vs_vwap": vs_vwap,
    }


def atr14(hist):
    done = strip_today(hist) if hist else hist
    if not done or len(done) < 16:
        return None
    trs = []
    for i in range(1, len(done)):
        h, l, pc = done[i][2], done[i][3], done[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < 14:
        return None
    return sum(trs[-14:]) / 14


def boll_pctb(hist, px, n=20, k=2):
    done = strip_today(hist) if hist else []
    c = [b[4] for b in done]
    if px is None or len(c) < n:
        return None
    m = sma(c, n)
    var = sum((x - m) ** 2 for x in c[-n:]) / n
    sd = var ** 0.5
    if not sd:
        return None
    return (px - (m - k * sd)) / (4 * sd)


def yhl_txt(hist, q):
    done = strip_today(hist) if hist else []
    if not done or not q:
        return "昨高低-"
    yh, yl = done[-1][2], done[-1][3]
    px = q["px"]
    if px > yh:
        return "过昨高"
    if px < yl:
        return "破昨低"
    return "昨高低内"


def orb_from_minutes(q, pts):
    out = {"orb": "ORB缺", "orb_hi": None, "orb_fake": False}
    if not q:
        return out
    if not pts:
        return out
    p15 = [p for t, p, *_ in pts if "09:30" <= t <= "09:44"]
    p30 = [p for t, p, *_ in pts if "09:30" <= t <= "09:59"]
    hi15 = max(p15) if p15 else None
    hi30 = max(p30) if p30 else None
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hm = now.strftime("%H:%M")
    hi = hi30 if hm >= "10:00" and hi30 else hi15
    if hi is None:
        out["orb"] = "ORB形成中"
        return out
    out["orb_hi"] = hi
    px, vwap = q["px"], q.get("vwap")
    below_vwap = vwap is not None and px < vwap
    if q["high"] >= hi * 0.999 and px < hi and below_vwap:
        out["orb"] = "假突"
        out["orb_fake"] = True
    elif px >= hi:
        out["orb"] = "过开盘高"
    elif px >= hi * 0.997:
        out["orb"] = "贴开盘高"
    else:
        out["orb"] = "未过开盘高"
    return out


def vol_pctile(hist, q):
    done = strip_today(hist) if hist else []
    vols = [b[5] for b in done[-20:] if b[5]]
    raw = hist[-1] if hist else None
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    tv = None
    if raw and raw[0] == today:
        tv = raw[5]
    if not vols:
        return "-"
    if tv is None:
        vr = (q.get("vol_ratio") if q else None) or 1
        v20 = sma(vols, 20) or sma(vols, len(vols))
        tv = vr * v20 if v20 else None
    if tv is None:
        return "-"
    pct = sum(1 for x in vols if x <= tv) / len(vols) * 100
    return f"量能分位{pct:.0f}%"


def consec_zt(code, hist):
    done = strip_today(hist) if hist else []
    if not done or len(done) < 2:
        return 0
    n = 0
    for i in range(len(done) - 1, 0, -1):
        prev, c = done[i - 1][4], done[i][4]
        if prev and prev > 0 and is_limit_up(code, (c / prev - 1) * 100):
            n += 1
        else:
            break
    return n


def yday_zt(code, hist):
    done = strip_today(hist) if hist else []
    if not done or len(done) < 2:
        return False
    prev, prev2 = done[-1][4], done[-2][4]
    if not prev2:
        return False
    return is_limit_up(code, (prev / prev2 - 1) * 100)


def auc_amt_txt(pts, hist):
    if not pts:
        return "竞价额-"
    first = pts[0]
    amt = first[3]
    vol = first[2]
    done = strip_today(hist) if hist else []
    v20 = sma([b[5] for b in done], 20) if done else None
    if v20 and vol:
        r = vol / (v20 / 240.0)
        r = min(r, 20)
        if r >= 3:
            return f"竞价量比{r:.1f}高"
        if r < 0.6:
            return f"竞价量比{r:.1f}低"
        return f"竞价量比{r:.1f}"
    if amt:
        return f"竞价额{amt/1e8:.2f}亿"
    return "竞价额-"


def auction_vol_ratio(pts, hist):
    """首分钟成交量 / (20日均量÷240)，代理竞价量比。封顶20。"""
    if not pts:
        return None
    vol = pts[0][2]
    done = strip_today(hist) if hist else []
    v20 = sma([b[5] for b in done], 20) if done else None
    if not v20 or not vol:
        return None
    return min(vol / (v20 / 240.0), 20.0)


def auction_judge(s, q, f, hist, yld=False):
    """自选集合竞价判断。主流量价：开幅+竞价量比+开后是否站开盘+位置。
    9:15-9:20可撤单噪声大；开盘价=9:25撮合结果。不进表一分、不进7因子。"""
    empty = {
        "call": "竞价缺", "gap": None, "vr": None, "vol_cls": "-",
        "why": "开盘/竞价数据暂缺", "tag": "竞价缺", "bits": [],
    }
    if not q or not q.get("prev") or q["prev"] <= 0:
        return empty
    o, p, prev = q.get("open") or 0, q["px"], q["prev"]
    if not o or o <= 0:
        return empty
    gap = (o / prev - 1) * 100
    pts = (f or {}).get("_min") or []
    vr = auction_vol_ratio(pts, hist)
    dd = (f or {}).get("dd")
    low_pos = dd is not None and dd <= -0.15
    high_pos = dd is not None and dd >= -0.05
    bits = [f"开{gap:+.2f}%"]
    if vr is not None:
        bits.append(f"竞价量比{vr:.1f}")

    if vr is None:
        vol_cls = "不明"
    elif vr >= 5:
        vol_cls = "巨量"
    elif vr >= 2.5:
        vol_cls = "放量"
    elif vr < 0.8:
        vol_cls = "缩量"
    else:
        vol_cls = "温和"
    bits.append(vol_cls)

    broken = p < o and (q["chg"] < gap - 0.4 or p < o * 0.997)
    held = p >= o * 0.999
    flipped = gap < 0 and q["chg"] > 0 and p >= prev
    amt0 = pts[0][3] if pts and len(pts[0]) > 3 else None
    if amt0:
        bits.append(f"竞价额{amt0/1e8:.2f}亿" if amt0 >= 1e8 else f"竞价额{amt0/1e4:.0f}万")

    call, why = "正常", "竞价中性，开后确认"
    # 一字/近涨停开盘。开后砸盘必须改口，不能一直挂「抢筹强」。
    if is_limit_up(s["code"], gap, q.get("name")) or gap >= board_limit_pct(s["code"], q.get("name")) * 0.9:
        if broken or (not held) or limit_open_dump(s, q):
            call, why = "骗炮警惕", "竞价涨停/近板开后砸盘，出货优先"
        elif vol_cls in ("缩量", "温和", "不明") or (vr is not None and vr < 2):
            call, why = "骗炮警惕", "近涨停开但竞价量不足，假强优先"
        else:
            call, why = "抢筹强", "竞价封涨停/近板且放量，开后不破开盘再认"
    elif gap >= 0.5:
        if vol_cls == "缩量" or (vol_cls == "温和" and gap >= 2.0):
            call, why = "骗炮警惕", "高开缩量，拉抬嫌疑"
        elif broken or (not held and q["chg"] < 0):
            call, why = "骗炮警惕", "高开后破开盘/走弱"
        elif vol_cls == "巨量" and high_pos:
            call, why = "骗炮警惕", "高位巨量高开，出货嫌疑"
        elif vol_cls in ("放量", "巨量"):
            if low_pos:
                call, why = "抢筹强", "低位高开放量"
            elif gap >= 7:
                call, why = "承接关注", "大幅高开放量，等开后3-5分钟承接"
            else:
                call, why = "抢筹强", "高开放量，开后站稳开盘再认"
        else:
            call, why = "承接关注", "高开温和量，看开后承接"
    elif gap <= -0.5:
        if vol_cls in ("放量", "巨量"):
            if flipped or (held and q["chg"] > 0):
                call, why = "承接关注", "低开放量后翻红，弱转强观察"
            else:
                call, why = "砸盘弱", "低开放量，抛压明确"
        else:
            if flipped:
                call, why = "承接关注", "低开缩量收回，情绪杀待确认"
            else:
                call, why = "砸盘弱", "低开偏弱"
    else:
        if vol_cls in ("放量", "巨量"):
            call, why = "承接关注", "平开巨量分歧，开后定方向"
        else:
            call, why = "正常", "平开竞价中性"

    dt = yday_dt_shape(q, yld) if yld else None
    if dt == "trap":
        call, why = "骗炮警惕", "昨跌停次日冲高回落骗炮"
        bits.append("昨跌停骗炮")
    elif dt == "turn":
        bits.append("昨跌停弱转强")
        if call in ("砸盘弱", "正常"):
            call, why = "承接关注", "昨跌停低开翻红"
    elif dt == "weak":
        bits.append("昨跌停偏弱")

    if low_pos:
        bits.append("低位")
    elif high_pos:
        bits.append("高位")

    return {
        "call": call, "gap": gap, "vr": vr, "vol_cls": vol_cls,
        "why": why, "tag": call, "bits": bits,
    }


def ma_slope_pct(closes, n=20, look=5):
    if len(closes) < n + look:
        return None
    a, b = sma(closes, n), sma(closes[:-look], n)
    if not a or not b:
        return None
    return (a / b - 1) * 100


def ret5(hist, px):
    if not hist or len(hist) < 5 or not px:
        return None
    return px / hist[-5][4] - 1


def rsi_turn_up(hist, q, f):
    c = [b[4] for b in hist] if hist else []
    prev = f.get("rsi") if f else rsi(c)
    live = rsi(c + [q["px"]]) if q and len(c) >= 15 else prev
    return prev is not None and live is not None and prev <= 38 and live >= prev + 2


def vol_dry_then_expand(hist, q):
    if not hist or not q or len(hist) < 8:
        return False
    v = [b[5] for b in hist]
    v20 = sma(v, 20)
    if not v20:
        return False
    dry = min(v[-5:]) <= v20 * 0.65
    return dry and (q.get("vol_ratio") or 0) >= 1.2


def double_bottom(hist, q, green):
    if not hist or not q or len(hist) < 8 or not green:
        return False
    prior = [b[3] for b in hist[-20:-1]]
    if not prior:
        return False
    swing = min(prior)
    return q["low"] >= swing * 0.985 and q["low"] <= swing * 1.03


def trend_annot(hist, q, f, flow=None, bench_r5=None, minutes=None):
    """表一标注：回踩/斜率/相对强度/量价/ATR仓位 + ORB/昨高/布林/换手分位。不进 buy 分，不进 name_call。"""
    out = {
        "slope_txt": "斜率-", "pullback": "无回踩", "rs_txt": "-", "vp": "量价-",
        "pos": "ATR-", "ma20_flat": True, "ma20_up": False, "slope": None, "r5": None,
        "orb": "ORB缺", "yhl": "昨高低-", "boll": "-", "hs_pct": "-", "board_rs": "-",
        "orb_fake": False, "atr_pct": None,
        "vwap_pos": "均价缺", "vwap_reclaim": False, "vwap_held": True, "vwap_key_low": None,
    }
    if not hist or not q or not f:
        return out
    c = [b[4] for b in hist]
    v = [b[5] for b in hist]
    px = q["px"]
    ma5, ma10, ma20 = f.get("ma5"), f.get("ma10"), f.get("ma20")
    slope = ma_slope_pct(c, 20, 5)
    out["slope"] = slope
    if slope is None:
        out["slope_txt"] = "斜率-"
        out["ma20_flat"] = True
    elif slope >= 0.4:
        out["slope_txt"] = f"MA20升{slope:.1f}%"
        out["ma20_up"] = True
        out["ma20_flat"] = True
    elif slope <= -0.8:
        out["slope_txt"] = f"MA20降{slope:.1f}%"
        out["ma20_flat"] = False
    else:
        out["slope_txt"] = f"MA20平{slope:.1f}%"
        out["ma20_flat"] = True

    near = None
    for name, m in (("MA5", ma5), ("MA10", ma10), ("MA20", ma20)):
        if m and abs(px / m - 1) <= 0.02:
            near = name
            break
    v20 = sma(v, 20)
    recent = sma(v[-5:], 5) if len(v) >= 5 else None
    shrink = bool(v20 and recent and recent < v20 * 0.85)
    was_above = bool(ma20 and len(c) >= 8 and sum(1 for x in c[-8:] if x > ma20) >= 4)
    vwap = q.get("vwap")
    above_vwap = vwap is not None and px >= vwap
    vr = q.get("vol_ratio") or 0
    if was_above and near and shrink and q["chg"] > 0 and above_vwap:
        out["pullback"] = f"回踩{near}承接"
    elif was_above and near:
        out["pullback"] = f"回踩{near}中"
    elif was_above and q["chg"] >= 2 and vr < 1:
        out["pullback"] = "无量冲"
    else:
        out["pullback"] = "无回踩"

    r5 = ret5(hist, px)
    out["r5"] = r5
    if r5 is not None and bench_r5 is not None:
        out["rs_txt"] = f"超额{(r5 - bench_r5) * 100:+.1f}%"
    elif r5 is not None:
        out["rs_txt"] = f"5日{r5 * 100:+.1f}%"

    hi10 = max(b[2] for b in hist[-10:]) if hist else None
    main = (flow or {}).get("main")
    if q["chg"] >= 0.3 and main is not None and main < 0:
        out["vp"] = "价涨资金出"
    elif hi10 and px >= hi10 * 0.998 and vr < 1:
        out["vp"] = "新高缩量"
    elif q["chg"] > 0.5 and vr < 0.8:
        out["vp"] = "价涨量缩"
    else:
        out["vp"] = "量价正常"

    atr = atr14(hist)
    if atr and px:
        atrp = atr / px * 100
        out["atr_pct"] = atrp
        stop = min(q["low"], px - atr)
        if atrp >= 4:
            out["pos"] = f"轻仓 ATR{atrp:.1f}% 破{stop:.2f}"
        elif atrp >= 2:
            out["pos"] = f"常规 ATR{atrp:.1f}% 破{stop:.2f}"
        else:
            out["pos"] = f"可略大 ATR{atrp:.1f}% 破{stop:.2f}"
    out.update(vwap_path(q, minutes))
    ob = orb_from_minutes(q, minutes)
    out["orb"] = ob["orb"]
    out["orb_fake"] = ob.get("orb_fake") or False
    out["yhl"] = yhl_txt(hist, q)
    bp = boll_pctb(hist, px)
    out["boll"] = f"%B{bp:.2f}" if bp is not None else "-"
    out["hs_pct"] = vol_pctile(hist, q)
    out["board_rs"] = "-"
    return out


def _pct_in(txt):
    if not txt:
        return None
    m = re.search(r"([+-]?\d+\.?\d*)%", str(txt))
    return float(m.group(1)) if m else None


def entry_score(f, q):
    """买点适合度。九列加权。不进表一 buy，不改 name_call / 可小仓原条件。"""
    if not f:
        return 0
    pb = f.get("pullback") or ""
    if "承接" in pb:
        s_pb = 100
    elif "回踩" in pb:
        s_pb = 70
    elif "无量冲" in pb:
        s_pb = 18
    else:
        s_pb = 40

    orb = f.get("orb") or ""
    if f.get("orb_fake") or orb == "假突":
        s_orb = 12
    elif orb == "过开盘高":
        s_orb = 72
    elif orb == "贴开盘高":
        s_orb = 68
    elif orb == "未过开盘高":
        s_orb = 55
    else:
        s_orb = 40

    yhl = f.get("yhl") or ""
    if yhl == "破昨低":
        s_yhl = 15
    elif yhl == "过昨高":
        s_yhl = 58
    elif yhl == "昨高低内":
        s_yhl = 82
    else:
        s_yhl = 50

    vp = f.get("vp") or ""
    if vp == "价涨资金出":
        s_vp = 15
    elif vp in ("价涨量缩", "新高缩量"):
        s_vp = 28
    elif vp == "量价正常":
        s_vp = 80
    else:
        s_vp = 50

    brs_v = _pct_in(f.get("board_rs"))
    s_brs = clip(50 + brs_v * 8, 0, 100) if brs_v is not None else 50
    rs_v = _pct_in(f.get("rs_txt"))
    s_rs = clip(50 + rs_v * 4, 0, 100) if rs_v is not None else 50

    s_boll = 50
    bm = re.search(r"%B(-?\d+\.?\d*)", str(f.get("boll") or ""))
    if bm:
        b = float(bm.group(1))
        if 0.30 <= b <= 0.80:
            s_boll = 88
        elif 0.20 <= b < 0.30 or 0.80 < b <= 0.90:
            s_boll = 62
        elif b > 1.0 or b < 0:
            s_boll = 18
        elif b > 0.90:
            s_boll = 32
        else:
            s_boll = 48

    s_hs = 50
    hm = re.search(r"(\d+)%", str(f.get("hs_pct") or ""))
    if hm:
        p = float(hm.group(1))
        if 40 <= p <= 80:
            s_hs = 88
        elif 25 <= p < 40 or 80 < p <= 90:
            s_hs = 62
        elif p < 15:
            s_hs = 25
        elif p >= 95:
            s_hs = 30
        else:
            s_hs = 50

    sl = f.get("slope")
    if sl is None:
        s_sl = 50
    elif sl >= 0.4:
        s_sl = 90
    elif sl >= 0:
        s_sl = 70
    elif sl >= -0.8:
        s_sl = 45
    else:
        s_sl = 22

    return (
        0.16 * s_pb + 0.14 * s_orb + 0.10 * s_yhl + 0.12 * s_vp
        + 0.12 * s_brs + 0.10 * s_rs + 0.09 * s_boll + 0.09 * s_hs + 0.08 * s_sl
    )


YOUZI_BOARDS = {
    "医药游资", "种业", "农业", "传媒", "广告", "零售", "消费", "软件",
    "化工", "液冷", "电子化学品", "超硬材料", "医药", "消费电子",
}
TREND_BOARDS = {"中药", "创新药", "PCB", "有色", "光伏", "服务器", "汽车", "电网", "电力", "医疗", "机器人", "热管理", "半导体", "玻纤"}
TREND_NAMES = {
    "沪电股份", "东山精密", "工业富联", "立讯精密", "中际旭创", "恒瑞医药",
    "紫金矿业", "上汽集团", "隆基绿能", "通富微电", "三花智控",
}


def mcap_yi(q):
    if not q or not q.get("mcap"):
        return None
    m = q["mcap"]
    return m / 10000.0 if m > 10000 else m


def recent_zt(s, hist, n=5):
    """近n日涨停次数（含昨收相对再昨）。"""
    done = strip_today(hist) if hist else []
    if not done or len(done) < 2:
        return 0
    bars = done[-(n + 1):]
    cnt = 0
    for i in range(1, len(bars)):
        prev, c = bars[i - 1][4], bars[i][4]
        if prev and prev > 0 and is_limit_up(s["code"], (c / prev - 1) * 100):
            cnt += 1
    return cnt


def youzi_tape_hits(s, q, hist=None):
    """券商/问财常用游资盘面：小市值、换手、振幅、量比、成交额、近端涨停、20cm。"""
    hits = []
    if not q:
        return hits
    yi = mcap_yi(q)
    hs = q.get("turnover")
    amp = q.get("amp") or 0
    vr = q.get("vol_ratio") or 0
    chg = q.get("chg") or 0
    amt = q.get("amt_wan") or 0
    if is_20cm(s["code"]):
        hits.append("20cm")
    if yi is not None and 20 <= yi <= 220:
        hits.append("小市值")
    if hs is not None and hs >= 5:
        hits.append("换手")
    if amp >= 4:
        hits.append("振幅")
    if vr >= 1.5:
        hits.append("量比")
    if amt >= 30000:
        hits.append("成交额")
    if chg >= 5:
        hits.append("冲高")
    if recent_zt(s, hist) >= 1:
        hits.append("近端涨停")
    return hits


BIG_CAP_YI = 400.0  # 超过这个市值的票不当游资标的，不管代码是不是 300/688


def stock_kind(s, q, hist=None):
    """分类看盘面和体量，名单只作弱先验。
    修正：过去 300/688 一律判游资，把新易盛/天孚这种千亿光模块票推进 7a 闸，
    和同一条主线的中际旭创（趋势闸）结论打架。现在大市值一律走趋势。"""
    if s.get("asset") == "etf" or "ETF" in (s.get("name") or ""):
        return "ETF"
    yi = mcap_yi(q)
    big = yi is not None and yi >= BIG_CAP_YI
    if s["name"] in TREND_NAMES:
        return "趋势"
    if big:
        return "趋势"
    if s.get("board") in TREND_BOARDS:
        return "趋势"
    if s.get("board") in YOUZI_BOARDS:
        return "游资"
    hits = youzi_tape_hits(s, q, hist)
    if is_20cm(s["code"]) and len(hits) >= 2:
        return "游资"
    if len(hits) >= 3:
        return "游资"
    hs = (q.get("turnover") if q else None) or 0
    amp = (q.get("amp") if q else 0) or 0
    vr = vr_norm((q.get("vol_ratio") if q else 0) or 0)
    if yi is not None and yi < 250 and (hs >= 5 or amp >= 5 or vr >= 1.8):
        return "游资"
    return "趋势"


def em_secid(s):
    return f"{0 if s['market'] == 'sz' else 1}.{s['code']}"


def stock_flow(stocks):
    """主力/超大单净流入代理。东财暗盘不是真成交，这是可复现口径。"""
    out = {}
    ids = [em_secid(s) for s in stocks]
    fields = "f12,f14,f62,f184,f66,f69,f164,f165"
    for i in range(0, len(ids), 18):
        chunk = ",".join(ids[i:i + 18])
        url = (
            "https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2&np=1&fields="
            + fields + "&secids=" + chunk
        )
        try:
            d = http(url, timeout=10)
        except Exception:
            continue
        for x in ((d.get("data") or {}).get("diff") or []):
            try:
                code = str(x["f12"]).zfill(6)
                out[code] = {
                    "main": float(x.get("f62") or 0),
                    "main_pct": float(x.get("f184") or 0),
                    "xlarge": float(x.get("f66") or 0),
                    "main5": float(x.get("f164") or 0),
                    "main5_pct": float(x.get("f165") or 0),
                }
            except Exception:
                continue
    return out


def youzi_score(s, q, f, yld, flow, inn_lines, out_lines, board_heat=None, hist=None):
    """7因子游资分（原8因子）。
    改动：今主力/5日主力/资金同向三项本来是同一个东财估算口径、合计占 0.47，
    现在合成一项占 0.30；腾出的权重给竞价、空间、低位，并新增一项「承接质量」
    （分时均价关系 + 封单额/封成比 + 开板次数）——这项和资金口径不相关，是真新增信息。
    量比/换手先做时段归一，早盘不再天然高分。"""
    chg = q["chg"] if q else 0
    vr_raw = q["vol_ratio"] if q else 0
    vr = vr_norm(vr_raw)
    amp = q["amp"] if q else 0
    mcap = q["mcap"] if q else 0
    yi = mcap / 10000.0 if mcap > 10000 else mcap
    dd = f["dd"] if f else -0.1
    line = line_of_board(s.get("board"))
    board = s.get("board") or ""
    main = (flow or {}).get("main") or 0
    main5 = (flow or {}).get("main5") or 0
    xlarge = (flow or {}).get("xlarge") or 0
    board_heat = board_heat or {}
    up_in, n_b = board_heat.get(board, (0, 0))

    # 1 板块资金：行业主力是否同向；光模块等允许「个股热钱自己干」
    if line in inn_lines or (n_b and up_in >= 2):
        s_sec = 90 if line in inn_lines else 82
        sec_txt = "板块流入" if line in inn_lines else f"{board}个股热钱同向"
        sec_mark = "同向"
    elif line in out_lines or (
        line in {"光通信", "PCB", "半导体", "算力液冷", "电子元件"} and "电子/科技" in out_lines
    ):
        s_sec = 38
        sec_txt = "板块流出"
        sec_mark = "背离"
    else:
        s_sec = 55
        sec_txt = "板块中性"
        sec_mark = "中性"

    # 2 主力5日 + 今主力（昨单日接口不稳，5日+今是代理）
    s_main_td = clip(50 + main / 1e8 * 3.5, 0, 100)
    s_main5 = clip(50 + main5 / 1e8 * 3, 0, 100)
    main5_mark = "5日进" if main5 > 0 else ("5日出" if main5 < 0 else "5日平")

    # 3 资金-涨幅同向：涨但大单出 = 出货嫌疑
    if chg >= 0.3 and main > 0:
        s_same, same_txt, same_mark = 100, "涨和主力同向", "同向"
    elif chg >= 0.3 and main < 0:
        s_same, same_txt, same_mark = 18, "涨但主力出/出货嫌疑", "出货"
    elif chg <= -0.3 and main < 0:
        s_same, same_txt, same_mark = 28, "跌和主力同向砸", "同砸"
    elif chg <= -0.3 and main > 0:
        s_same, same_txt, same_mark = 58, "价弱单进", "背离接"
    else:
        s_same, same_txt, same_mark = 50, "资金涨幅不明显", "不明"

    # 4 游资弹性：小市值+高量比+高振幅；20cm加分
    size = clip(120 - yi / 20, 0, 100)
    if is_20cm(s["code"]):
        size = max(size, 52)
    s_elast = 0.45 * clip(vr * 18, 0, 100) + 0.35 * clip(amp * 10, 0, 100) + 0.20 * size
    if is_20cm(s["code"]):
        s_elast = clip(s_elast + 12, 0, 100)
    elast_mark = "高" if s_elast >= 70 else ("中" if s_elast >= 45 else "低")

    # 5 涨停空间
    cap = board_limit_pct(s["code"], q.get("name") if q else None)
    room = cap - chg
    s_room = clip(room / cap * 100, 0, 100)
    room_mark = "见顶" if room <= cap * 0.1 else ("紧" if room <= cap * 0.3 else "足")

    # 6 竞价质量：高开低走骗炮；昨跌停看形态，不一律骗炮
    o, p, prev = q["open"], q["px"], q["prev"]
    gap = (o / prev - 1) * 100 if prev else 0
    dt = yday_dt_shape(q, yld) if yld else None
    if dt == "trap":
        s_auc, auc_txt, auc_mark = 8, "昨跌停骗炮", "骗炮"
    elif dt == "turn":
        s_auc, auc_txt, auc_mark = 82, "昨跌停低开翻红", "翻红"
    elif dt == "weak":
        s_auc, auc_txt, auc_mark = 28, "昨跌停次日偏弱", "走弱"
    elif gap >= 0.8 and p < o and chg < 0:
        s_auc, auc_txt, auc_mark = 12, "高开低走骗炮", "骗炮"
    elif gap >= 0.5 and p < o:
        s_auc, auc_txt, auc_mark = 28, "高开走弱", "走弱"
    elif gap <= -0.3 and chg > 0:
        s_auc, auc_txt, auc_mark = 82, "低开翻红", "翻红"
    else:
        s_auc, auc_txt, auc_mark = 62, "竞价正常", "正常"

    # 7 低位启动：离前高远、今天突然转强
    s_low = clip((-dd) * 220, 0, 100)
    if chg >= 2 and dd is not None and dd <= -0.15:
        s_low = clip(s_low + 12, 0, 100)
    low_mark = "有" if (dd is not None and dd <= -0.12 and chg >= 2) else ("远" if dd is not None and dd <= -0.2 else "无")

    # 8 承接质量：分时均价关系 + 封单/开板。和资金口径不相关，是新增的独立信息
    seal = seal_quality(q, s["code"])
    pts = (f or {}).get("_min") or []
    n_open = open_times(pts, q.get("prev"), s["code"], q.get("name")) if pts else None
    vwap_q = q.get("vwap")
    s_hold = 50
    hold_bits = []
    if vwap_q and p:
        dev = (p / vwap_q - 1) * 100
        if dev >= 0.5:
            s_hold, _t = 82, "均价上方"
        elif dev >= -0.1:
            s_hold, _t = 68, "贴均价"
        elif dev >= -0.8:
            s_hold, _t = 38, "均价下方"
        else:
            s_hold, _t = 18, "远离均价下"
        hold_bits.append(_t)
        if (f or {}).get("vwap_reclaim") and (f or {}).get("vwap_held"):
            s_hold = clip(s_hold + 10, 0, 100)
            hold_bits.append("破后站回")
    if seal.get("at_limit"):
        r = seal.get("seal_ratio")
        if r is not None:
            if r >= 0.5:
                s_hold = clip(max(s_hold, 88), 0, 100)
                hold_bits.append("硬板")
            elif r >= 0.15:
                s_hold = clip(max(s_hold, 68), 0, 100)
                hold_bits.append("封单一般")
            else:
                s_hold = min(s_hold, 32)
                hold_bits.append("弱板")
    if n_open is not None and n_open >= 1:
        s_hold = clip(s_hold - 12 * min(n_open, 3), 0, 100)
        hold_bits.append(f"开板{n_open}次")
    hold_mark = "/".join(hold_bits) if hold_bits else "均价缺"

    # 追高罚：按本板涨停幅度折算，20cm 不再按 10cm 的尺子罚
    chase = 0
    chase_bits = []
    oh = overheat(s["code"], chg, q.get("name") if q else None)
    if oh in ("涨停", "见顶"):
        chase += 40
        chase_bits.append(f"{oh}罚")
    elif oh == "不追":
        chase += 22
        chase_bits.append("过热罚")
    elif oh == "偏热":
        chase += 12
        chase_bits.append("偏热罚")
    if vr >= 8:
        chase += 10
        chase_bits.append("量比爆")
    if dt == "trap":
        chase += 35
        chase_bits.append("昨跌停骗炮")
    elif dt == "weak":
        chase += 18
        chase_bits.append("昨跌停偏弱")
    elif dt == "turn":
        chase += 8
        chase_bits.append("昨跌停弱转强")
    chase_mark = ",".join(chase_bits) if chase_bits else "无"

    # 资金三项合成一块，避免同一个东财估算口径占掉近一半权重
    s_money = 0.45 * s_main_td + 0.20 * s_main5 + 0.35 * s_same
    score = (
        0.10 * s_sec + 0.30 * s_money + 0.18 * s_elast
        + 0.10 * s_room + 0.12 * s_auc + 0.10 * s_low + 0.10 * s_hold
        - chase
    )
    score = clip(score, 0, 100)

    vwap = q.get("vwap")
    low = q["low"]
    is_etf = s.get("asset") == "etf" or "ETF" in (s.get("name") or "")
    if is_etf:
        if chg >= 3:
            how = "ETF日内已冲，跟踪不追"
        elif score >= 60 and main > 0:
            how = "ETF跟主线，可小仓"
        elif score >= 50:
            how = "ETF观察"
        else:
            how = "ETF不做"
    elif is_limit_up(s["code"], chg, q.get("name")):
        how = "涨停，结束/观察"
    elif oh == "见顶":
        how = "空间见顶，不追"
    elif auc_mark == "骗炮":
        how = "昨跌停骗炮，不做" if dt == "trap" else "高开低走骗炮，不做"
    elif room_mark == "见顶":
        how = "空间见顶，不追"
    elif same_mark == "出货":
        how = "涨但主力出，降级不追"
    elif sec_mark == "背离" and score >= 60:
        how = "板块背离，分歧区快进快出"
    elif score >= 75 and main > 0:
        how = f"主线。回抽 {low:.0f}–{(vwap or q['px']):.0f} 接，别追尖"
    elif score >= 65:
        how = "快进快出，仓要小"
    elif score >= 55:
        how = "观察回抽，不追"
    elif score >= 40:
        how = "降级，今天游资不做主仓"
    else:
        how = "不做"

    flow_txt = f"今主力{main/1e8:+.1f}亿"
    if xlarge:
        flow_txt += f" 超大单{xlarge/1e8:+.1f}亿"
    if main5:
        flow_txt += f" 5日{main5/1e8:+.1f}亿"
    factor_line = (
        f"板块{sec_mark}；资金{same_mark}({main5_mark})；弹性{elast_mark}；"
        f"空间{room_mark}；竞价{auc_mark}；低位{low_mark}；承接{hold_mark}；追高{chase_mark}"
    )
    return {
        "score": score, "kind": stock_kind(s, q, hist), "how": how, "board": board,
        "flow_txt": flow_txt + " " + same_txt,
        "sec_txt": sec_txt, "auc_txt": auc_txt, "factor_line": factor_line,
        "elast": s_elast, "elast_mark": elast_mark, "room": room, "cap": cap,
        "main": main, "main5": main5, "xlarge": xlarge,
        "main_pct": (flow or {}).get("main_pct") or 0,
        "same_txt": same_txt, "same_mark": same_mark,
        "hold": s_hold, "hold_mark": hold_mark, "seal": seal, "n_open": n_open,
        "overheat": oh, "vr_norm": vr, "vr_raw": vr_raw,
        "marks": {
            "板块资金": sec_mark, "资金合成": same_mark, "主力5日": main5_mark,
            "游资弹性": elast_mark, "涨停空间": room_mark, "竞价质量": auc_mark,
            "低位启动": low_mark, "承接质量": hold_mark, "追高罚": chase_mark,
        },
    }


def sector_flow():
    urls = [
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2&fields=f14,f3,f62,f184,f204&ut=fa5fd1943c7b386f172d6893dbfba10b",
        "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2&fields=f14,f3,f62,f184,f204&ut=fa5fd1943c7b386f172d6893dbfba10b",
    ]
    urls_out = [
        "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=6&po=0&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2&fields=f14,f3,f62,f184&ut=fa5fd1943c7b386f172d6893dbfba10b",
        "https://push2delay.eastmoney.com/api/qt/clist/get?pn=1&pz=6&po=0&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2&fields=f14,f3,f62,f184&ut=fa5fd1943c7b386f172d6893dbfba10b",
    ]
    inn, out = [], []
    for url in urls:
        try:
            d = http(url, timeout=8)
            for x in (d.get("data") or {}).get("diff") or []:
                inn.append(f"{x['f14']} {x['f3']:+}% 主力{float(x['f62'])/1e8:+.1f}亿 领{x.get('f204')}")
            if inn:
                break
        except Exception:
            continue
    for url in urls_out:
        try:
            d2 = http(url, timeout=8)
            for x in (d2.get("data") or {}).get("diff") or []:
                out.append(f"{x['f14']} {x['f3']:+}% 主力{float(x['f62'])/1e8:+.1f}亿")
            if out:
                break
        except Exception:
            continue
    return inn, out


def parse_sector_line(row):
    """东财板块资金一行 → 表列。flow_sets 仍吃原文。"""
    s = str(row or "").strip()
    m = re.search(
        r"^(.*?)\s+([+-]?\d+(?:\.\d+)?)%\s+主力([+-]?\d+(?:\.\d+)?)亿(?:\s+领(.*))?$",
        s,
    )
    if not m:
        return {"name": (s.split() or ["-"])[0], "chg": None, "amt": None, "lead": "-"}
    return {
        "name": m.group(1).strip(),
        "chg": float(m.group(2)),
        "amt": float(m.group(3)),
        "lead": (m.group(4) or "-").strip() or "-",
    }


def _yahoo_bar(sym):
    """Return (last, prev, chg_pct) or None. 不用 http()：东财 Referer 会被 Yahoo 拒。"""
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=10d",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=10d",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    for url in urls:
        try:
            req = urllib.request.Request(url, headers=headers)
            data = json.loads(urllib.request.urlopen(req, timeout=10).read())
            res = data["chart"]["result"][0]
            q = res["indicators"]["quote"][0]
            closes = [c for c in q["close"] if c is not None]
            if len(closes) < 2:
                continue
            last, prev = closes[-1], closes[-2]
            return last, prev, (last / prev - 1) * 100
        except Exception:
            continue
    return None


OVN_CACHE = os.path.join(ROOT, "reports", "overnight_cache.json")


def _save_ovn_cache(scan):
    if not (scan.get("indices") or scan.get("leaders") or scan.get("themes")):
        return
    try:
        os.makedirs(os.path.dirname(OVN_CACHE), exist_ok=True)
        blob = dict(scan)
        blob["_saved"] = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        ).isoformat()
        json.dump(blob, open(OVN_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def _load_ovn_cache(max_hours=36):
    try:
        blob = json.load(open(OVN_CACHE, encoding="utf-8"))
        ts = blob.get("_saved") or ""
        saved = datetime.datetime.fromisoformat(ts)
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=now.tzinfo)
        if (now - saved).total_seconds() > max_hours * 3600:
            return None
        blob["_from_cache"] = True
        return blob
    except Exception:
        return None


# 美股领涨主题 → A股自选 board（watchlist.json 的 board 字段）
US_THEME_MAP = [
    {
        "theme": "半导体/算力硬件",
        "syms": [("^SOX", "费城半导体"), ("SMH", "半导体ETF"), ("XLK", "科技ETF")],
        "boards": ["半导体", "电子化学品", "元件", "服务器", "PCB", "电子元件", "算力硬件"],
        "weight": 1.0,
    },
    {
        "theme": "光通信/CPO",
        "syms": [("LITE", "Lumentum"), ("COHR", "Coherent"), ("SMH", "半导体ETF")],
        "boards": ["光模块", "光纤", "通信线缆", "光通信"],
        "weight": 1.1,
    },
    {
        "theme": "算力液冷/热管理",
        "syms": [("SMH", "半导体ETF"), ("XLK", "科技ETF"), ("BOTZ", "机器人ETF")],
        "boards": ["液冷", "热管理"],
        "weight": 0.9,
    },
    {
        "theme": "生物科技/医药",
        "syms": [("IBB", "生科ETF"), ("XLV", "医疗ETF")],
        "boards": ["医药游资", "医药", "创新药", "医疗", "中药"],
        "weight": 1.0,
    },
    {
        "theme": "有色/工业金属",
        "syms": [("HG=F", "铜"), ("SI=F", "白银"), ("GC=F", "黄金"), ("XLB", "材料ETF")],
        "boards": ["有色", "超硬材料"],
        "weight": 1.0,
    },
    {
        "theme": "原油/能源链",
        "syms": [("CL=F", "WTI原油"), ("BZ=F", "布伦特"), ("XLE", "能源ETF")],
        "boards": ["化工"],
        "weight": 0.85,
    },
    {
        "theme": "新能源/光伏",
        "syms": [("TAN", "太阳能ETF")],
        "boards": ["光伏", "电力", "电网", "电网设备"],
        "weight": 0.95,
    },
    {
        "theme": "消费/传媒",
        "syms": [("XLY", "可选消费"), ("XLC", "通信服务")],
        "boards": ["消费", "传媒", "广告", "零售"],
        "weight": 0.8,
    },
    {
        "theme": "机器人/自动化",
        "syms": [("BOTZ", "机器人ETF")],
        "boards": ["机器人"],
        "weight": 0.9,
    },
    {
        "theme": "软件/科技应用",
        "syms": [("XLK", "科技ETF"), ("XLC", "通信服务")],
        "boards": ["软件", "传媒"],
        "weight": 0.85,
    },
]


def overnight_scan(stocks=None, etfs=None):
    """隔夜美股指数+板块ETF+金属原油扫描，并映射到自选次日关注。"""
    idx_specs = [
        ("^DJI", "道指"), ("^GSPC", "标普"), ("^IXIC", "纳指"), ("^SOX", "费城半导体"),
        ("^N225", "日经"), ("^HSI", "恒生"),
    ]
    sector_specs = [
        ("SMH", "半导体ETF"), ("XLK", "科技ETF"), ("XLF", "金融ETF"),
        ("XLE", "能源ETF"), ("XLV", "医疗ETF"), ("IBB", "生科ETF"),
        ("XLB", "材料ETF"), ("XLI", "工业ETF"), ("XLY", "可选消费"),
        ("XLC", "通信服务"), ("XLP", "必需消费"), ("XLU", "公用事业"),
        ("BOTZ", "机器人ETF"), ("TAN", "太阳能ETF"),
    ]
    metal_specs = [
        ("CL=F", "WTI原油"), ("BZ=F", "布伦特"),
        ("GC=F", "黄金"), ("SI=F", "白银"), ("HG=F", "铜"),
        ("DX-Y.NYB", "美元指数"), ("^TNX", "美债10Y"),
    ]
    related_specs = [("LITE", "Lumentum"), ("COHR", "Coherent")]
    all_specs = idx_specs + sector_specs + metal_specs + related_specs
    by_sym = {}

    def one(sym, name):
        bar = _yahoo_bar(sym)
        if not bar:
            return None
        last, prev, chg = bar
        return {"sym": sym, "name": name, "last": last, "prev": prev, "chg": chg}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(one, s, n): (s, n) for s, n in all_specs}
        for fut in as_completed(futs):
            row = fut.result()
            if row:
                by_sym[row["sym"]] = row

    def fmt_row(row):
        if row["sym"] == "^TNX":
            return f"{row['name']} {row['last']:.3f}% {row['chg']:+.1f}bp"
        if row["name"] in ("WTI原油", "布伦特", "黄金", "白银", "铜", "美元指数"):
            return f"{row['name']} {row['last']:.2f} {row['chg']:+.2f}%"
        return f"{row['name']} {row['last']:.2f} {row['chg']:+.2f}%"

    summary = [fmt_row(by_sym[s]) for s, _ in all_specs if s in by_sym]

    def pack(specs):
        rows = [by_sym[s] for s, _ in specs if s in by_sym]
        rows.sort(key=lambda r: -r["chg"])
        return rows

    indices = pack(idx_specs)
    sectors = pack(sector_specs)
    metals = pack(metal_specs)
    related = pack(related_specs)

    # 主题得分：相关标的涨跌均值 × 权重
    themes = []
    for tm in US_THEME_MAP:
        parts = [by_sym[s]["chg"] for s, _ in tm["syms"] if s in by_sym]
        if not parts:
            continue
        raw = sum(parts) / len(parts)
        score = raw * tm["weight"]
        detail = "；".join(
            f"{n}{by_sym[s]['chg']:+.2f}%" for s, n in tm["syms"] if s in by_sym
        )
        themes.append({
            "theme": tm["theme"], "score": score, "raw": raw,
            "boards": tm["boards"], "detail": detail,
        })
    themes.sort(key=lambda t: -t["score"])
    # 相对领涨：夜里指数平淡时 0.4 阈值会把整表打空。始终给出前几名，标态度。
    if themes:
        top_sc = themes[0]["score"]
        cut = 0.35 if top_sc >= 0.55 else min(0.12, top_sc - 0.05) if top_sc > 0 else -0.05
        leaders = [t for t in themes if t["score"] >= cut][:5] or themes[:3]
        for t in leaders:
            if t["score"] >= 0.8:
                t["tag"] = "领涨"
            elif t["score"] >= 0.35:
                t["tag"] = "偏强"
            elif t["score"] >= 0:
                t["tag"] = "相对强"
            else:
                t["tag"] = "一般"
    else:
        leaders = []
    laggers = [t for t in themes if t["score"] <= -0.35]
    laggers.sort(key=lambda t: t["score"])

    # 风险偏好
    idx_chgs = [by_sym[s]["chg"] for s in ("^DJI", "^GSPC", "^IXIC") if s in by_sym]
    avg_idx = sum(idx_chgs) / len(idx_chgs) if idx_chgs else 0
    dxy = by_sym.get("DX-Y.NYB", {}).get("chg")
    oil = by_sym.get("CL=F", {}).get("chg")
    if avg_idx <= -0.8:
        bias, bias_why = "风险规避", "美股三大指数隔夜偏弱，A股高开宜谨慎"
    elif avg_idx >= 0.8 and (dxy is None or dxy < 0.3):
        bias, bias_why = "风险偏好", "美股隔夜偏强且美元未大幅走强，科技/成长可对照"
    elif avg_idx >= 0.3:
        bias, bias_why = "温和偏多", "指数小涨，看领涨板块映射，不追高开"
    else:
        bias, bias_why = "中性分化", "指数一般，只跟领涨主题，不看大盘情绪"

    stocks = stocks or []
    etfs = etfs or []
    # 按主线归并：光模块/光纤/通信线缆 都算光通信，避免 board 字面不一致把表打空
    line_names = {}
    for s in stocks:
        b = s.get("board") or ""
        ln = BOARD_LINE.get(b, b)
        line_names.setdefault(ln, []).append((s["name"], b or ln))
    etf_by_line = {}
    for e in etfs:
        ln = ETF_LINE.get(e["name"], "")
        if ln:
            etf_by_line.setdefault(ln, []).append(e["name"])

    picks = []
    seen = set()
    for t in leaders:
        mapped = []
        for b in t["boards"]:
            ln = BOARD_LINE.get(b, b)
            if ln not in mapped:
                mapped.append(ln)
        for ln in mapped:
            for name, board in line_names.get(ln, []):
                if name in seen:
                    continue
                seen.add(name)
                attitude = "优先盯" if t["score"] >= 1.2 else "可关注"
                picks.append({
                    "name": name, "board": board, "line": ln, "theme": t["theme"],
                    "score": t["score"], "detail": t["detail"],
                    "attitude": attitude, "kind": "股票",
                })
            for en in etf_by_line.get(ln, []):
                if en in seen:
                    continue
                seen.add(en)
                picks.append({
                    "name": en, "board": ln, "line": ln, "theme": t["theme"],
                    "score": t["score"] * 0.95, "detail": t["detail"],
                    "attitude": "ETF对照", "kind": "ETF",
                })
    picks.sort(key=lambda p: (-p["score"], p["name"]))
    picks = picks[:12]

    avoid = []
    for t in laggers[:4]:
        mapped = {BOARD_LINE.get(b, b) for b in t["boards"]}
        for ln in mapped:
            for name, board in line_names.get(ln, [])[:3]:
                avoid.append({
                    "name": name, "board": board, "line": ln, "theme": t["theme"],
                    "score": t["score"], "detail": t["detail"],
                })

    oil_note = ""
    if oil is not None:
        if oil >= 2:
            oil_note = f"原油大涨{oil:+.2f}%，通胀预期升温，成长估值受压、化工/能源链可对照"
        elif oil <= -2:
            oil_note = f"原油大跌{oil:+.2f}%，成本端松一点，但能源链映射偏弱"

    return {
        "summary": summary,
        "indices": indices,
        "sectors": sectors,
        "metals": metals,
        "related": related,
        "themes": themes,
        "leaders": leaders,
        "laggers": laggers,
        "bias": bias,
        "bias_why": bias_why,
        "picks": picks,
        "avoid": avoid[:8],
        "oil_note": oil_note,
        "avg_idx": avg_idx,
    }


def _seed_themes_from_macro():
    """Yahoo 空时，用宏观笔记里的隔夜盘面文字补主题，避免 1b 整表空白。"""
    blk = MACRO.get("overnight_external") or {}
    blob = "。".join(str(x) for x in (blk.get("markets") or []) + (blk.get("policy") or []))
    out = []
    if any(k in blob for k in ("Lumentum", "Coherent", "光通信美股")):
        strong = any(k in blob for k in ("反而强", "夜间强", "偏强", "并未崩溃"))
        out.append({
            "theme": "光通信/CPO",
            "score": 0.55 if strong else 0.15,
            "raw": 0.55 if strong else 0.15,
            "boards": ["光模块", "光纤", "通信线缆", "光通信"],
            "detail": "宏观笔记：光通信美股(LITE/COHR)隔夜有记述（实时报价暂缺）",
            "tag": "相对强" if strong else "一般",
        })
    if "费城半导体" in blob and any(k in blob for k in ("并未崩溃", "未崩溃", "偏强")):
        out.append({
            "theme": "半导体/算力硬件",
            "score": 0.25,
            "raw": 0.25,
            "boards": ["半导体", "电子化学品", "元件", "服务器", "PCB"],
            "detail": "宏观笔记：费城半导体隔夜未崩（实时报价暂缺）",
            "tag": "一般",
        })
    return out


def overnight_quotes():
    """兼容旧调用：返回隔夜报价字符串列表。"""
    return overnight_scan().get("summary") or []


ETF_LINE = {
    "港股创新药ETF": "创新药", "创新药ETF东财": "创新药", "粮食ETF": "农业",
    "科创半导体ETF": "半导体", "科创50ETF东财": "半导体",
    "恒生科技ETF": "电子/科技", "有色金属ETF": "有色",
}

BOARD_LINE = {
    "液冷": "算力液冷", "热管理": "热管理", "服务器": "算力硬件",
    "光模块": "光通信", "光纤": "光通信", "通信线缆": "光通信", "光通信": "光通信", "PCB": "PCB",
    "半导体": "半导体", "电子化学品": "半导体", "元件": "电子元件",
    "消费电子": "消费电子",
    "医药游资": "游资医药", "医药": "医药", "中药": "中药",
    "创新药": "创新药", "医疗": "医疗",
    "汽车": "汽车", "有色": "有色", "超硬材料": "有色", "玻纤": "玻纤",
    "农业": "农业", "种业": "农业", "粮食": "农业",
    "电网": "电网", "电网设备": "电网", "电力": "电力", "光伏": "光伏",
    "传媒": "传媒", "广告": "传媒", "消费": "消费", "零售": "消费",
    "化工": "化工", "软件": "软件", "机器人": "机器人",
}

BOARD_BENCH = {
    "光模块": "515050", "光纤": "515050",
    "PCB": "512480", "半导体": "512480", "电子化学品": "512480",
    "液冷": "512480", "热管理": "000001", "服务器": "512480", "元件": "512480",
    "玻纤": "000001", "通信线缆": "515050",
    "消费电子": "399006",
    "创新药": "512010", "医药": "512010", "医药游资": "512010", "中药": "512010", "医疗": "512010",
    "有色": "512400", "超硬材料": "512400",
    "农业": "159698", "种业": "159698", "粮食": "159698",
    "光伏": "399006", "传媒": "399006", "广告": "399006", "软件": "399006", "机器人": "399006",
    "汽车": "000001", "电网": "000001", "电网设备": "000001", "电力": "000001",
    "化工": "000001", "零售": "000001", "消费": "000001",
}

BOARD_HY_KEYS = {
    "液冷": ["专用设备", "计算机设备", "自动化"],
    "热管理": ["专用设备", "汽车零部件", "制冷"],
    "服务器": ["计算机设备", "消费电子"],
    "光模块": ["通信设备", "光学光电子", "通信"],
    "光纤": ["通信设备", "光纤光缆", "通信"],
    "通信线缆": ["通信设备", "光纤光缆", "通信"],
    "玻纤": ["玻纤", "玻璃陶瓷", "玻璃玻纤", "非金属"],
    "PCB": ["元件", "印刷电路板", "电子"],
    "半导体": ["半导体"],
    "电子化学品": ["化学制品", "半导体"],
    "元件": ["元件"],
    "消费电子": ["消费电子"],
    "医药游资": ["化学制药", "中药", "生物制品", "医疗"],
    "医药": ["化学制药", "生物制品"],
    "中药": ["中药"],
    "创新药": ["化学制药", "生物制品"],
    "医疗": ["医疗器械", "医疗服务"],
    "汽车": ["汽车"],
    "有色": ["工业金属", "小金属", "贵金属", "能源金属"],
    "超硬材料": ["非金属", "工业金属"],
    "农业": ["农产品", "养殖", "种植", "饲料"],
    "种业": ["种植"],
    "粮食": ["农产品"],
    "电网": ["电网设备"],
    "电网设备": ["电网设备"],
    "电力": ["电力"],
    "光伏": ["光伏", "电源设备"],
    "传媒": ["影视", "互联网传媒", "游戏"],
    "广告": ["广告营销"],
    "消费": ["零售", "商超", "食品"],
    "零售": ["零售", "百货"],
    "化工": ["化学制品", "化学原料"],
    "软件": ["软件开发", "IT服务"],
    "机器人": ["自动化设备", "专用设备"],
}

FLOW_LINE = [
    ("汽车零部件", "汽车"), ("底盘", "汽车"), ("汽车", "汽车"),
    ("光学光电子", "面板"), ("面板", "面板"),
    ("医药生物", "医药"), ("医药", "医药"),
    ("半导体", "半导体"),
    ("印制电路板", "PCB"), ("印刷电路板", "PCB"),
    ("元件", "电子元件"),
    ("电子", "电子/科技"),
    ("有色金属", "有色"), ("工业金属", "有色"),
    ("通信线缆", "通信线缆"), ("光纤光缆", "通信线缆"),
    ("玻纤制造", "玻纤"), ("玻璃玻纤", "玻纤"), ("玻纤", "玻纤"), ("玻璃陶瓷", "玻纤"),
    ("通信网络设备", "光通信"), ("通信设备", "光通信"),
    ("光模块", "光通信"),
    ("通信", "光通信"),
    ("计算机", "软件"), ("软件", "软件"),
    ("国防", "军工"), ("军工", "军工"),
    ("农业", "农业"), ("种业", "农业"),
    ("电网", "电网"), ("电力", "电力"),
]


def sector_token(row):
    return (row or "").split()[0] if row else ""


def line_of_board(board):
    return BOARD_LINE.get(board, board or "其他")


def overnight_follow_desk(ovn, stocks, etfs, rows, etf_rows, yz_by_code, inn, outf, inn_lines, out_lines):
    """隔夜主题 → 次日国内关注板块 → 自选龙头预案。只写 1b，不进买点闸/表一/7a。"""
    ovn = dict(ovn or {})
    themes = list(ovn.get("themes") or [])
    leaders = list(ovn.get("leaders") or [])
    if not leaders:
        if not themes:
            themes = _seed_themes_from_macro()
            ovn["themes"] = themes
        leaders = [t for t in themes if t.get("score", 0) >= -0.05][:5] or themes[:3]
        for t in leaders:
            t.setdefault("tag", "相对强" if t.get("score", 0) >= 0 else "一般")
        ovn["leaders"] = leaders
        if leaders and not ovn.get("indices"):
            extra = "实时报价暂缺，主题来自宏观笔记或缓存"
            if extra not in (ovn.get("bias_why") or ""):
                ovn["bias_why"] = ((ovn.get("bias_why") or "指数一般，只跟领涨主题") + "；" + extra)

    heat = flow_heat_map(inn, outf)
    desk = set(desk_lines_of(stocks, etfs))

    def mapped_lines(theme):
        lines = []
        for b in theme.get("boards") or []:
            ln = line_of_board(b)
            if ln and ln not in lines:
                lines.append(ln)
        return lines

    day_boards = []
    seen_ln = set()
    for t in leaders[:6]:
        for ln in mapped_lines(t):
            if ln in seen_ln:
                continue
            seen_ln.add(ln)
            h = heat.get(ln) or {}
            if ln in inn_lines:
                money = f"资金确认 主力{(h.get('amt') or 0):+.0f}亿"
            elif ln in out_lines:
                money = f"资金流出 主力{(h.get('amt') or 0):+.0f}亿"
            else:
                money = "资金未对上"
            in_desk = ln in desk
            sc = t.get("score") or 0
            if not in_desk:
                att = "池外主题"
            elif ln in out_lines and sc < 0.8:
                att = "只记录"
            elif sc >= 0.8 or (sc >= 0.2 and ln in inn_lines):
                att = "优先盯"
            else:
                att = "可关注"
            day_boards.append({
                "line": ln,
                "theme": t.get("theme") or "-",
                "score": sc,
                "tag": t.get("tag") or "-",
                "boards": "/".join(t.get("boards") or []),
                "money": money,
                "in_desk": in_desk,
                "attitude": att,
                "why": f"隔夜{t.get('theme')}{sc:+.2f}（{t.get('detail') or '-'}）；A股{money}",
            })

    if not day_boards:
        for ln in inn_lines:
            if ln not in desk:
                continue
            h = heat.get(ln) or {}
            day_boards.append({
                "line": ln,
                "theme": "隔夜报价暂缺",
                "score": 0,
                "tag": "国内资金",
                "boards": ln,
                "money": f"主力流入{(h.get('amt') or 0):+.0f}亿",
                "in_desk": True,
                "attitude": "可关注",
                "why": "隔夜主题空，改看当日/昨日资金流入主线，次日先盯不追",
            })

    watch_lines = {
        d["line"] for d in day_boards
        if d["in_desk"] and d["attitude"] in ("优先盯", "可关注")
    }
    if not watch_lines:
        watch_lines = {d["line"] for d in day_boards if d["in_desk"]}
    theme_of = {}
    for d in day_boards:
        theme_of.setdefault(d["line"], d)

    def pack_row(s, q, f, kind):
        ln = line_of_board(s.get("board"))
        if s.get("asset") == "etf" or "ETF" in (s.get("name") or ""):
            ln = ETF_LINE.get(s.get("name") or "", ln)
        if ln not in watch_lines:
            return None
        yz = (yz_by_code or {}).get(s.get("code")) or {}
        chg = (q or {}).get("chg")
        if chg is None:
            return None
        buy = ((f or {}).get("buy") or 0)
        yz_sc = yz.get("score") or 0
        rank = (f or {}).get("board_rank") or ""
        pos = 99
        m = re.search(r"板内(\d+)/", str(rank))
        if m:
            pos = int(m.group(1))
        lead_bonus = max(0, 10 - pos * 2)
        tb = theme_of.get(ln) or {}
        tsc = tb.get("score") or 0
        rank_sc = tsc * 10 + (chg or 0) + lead_bonus + yz_sc * 0.1 + buy * 0.05
        if pos <= 2:
            role = "板内龙头"
        elif pos <= 4:
            role = "板内靠前"
        else:
            role = "主题对照"
        att = "优先盯" if (tb.get("attitude") == "优先盯" and (pos <= 2 or chg >= 2)) else "可关注"
        why = (
            f"{tb.get('theme') or ln}隔夜{tsc:+.2f}；{rank or '板内-'} 今{chg:+.2f}%；"
            f"{tb.get('money') or ''}。预案，开盘后过闸才算"
        )
        return {
            "name": s.get("name"),
            "code": s.get("code"),
            "kind": kind,
            "board": s.get("board") or ln,
            "line": ln,
            "theme": tb.get("theme") or "-",
            "score": tsc,
            "detail": tb.get("why") or (tb.get("theme") or ""),
            "attitude": att,
            "role": role,
            "chg": chg,
            "rank": rank or "-",
            "yz": yz_sc,
            "buy": buy,
            "rank_sc": rank_sc,
            "why": why,
        }

    cands = []
    for s, q, f, yld, hist in rows or []:
        row = pack_row(s, q, f, "股票")
        if row:
            cands.append(row)
    for s, q, f, yld, hist in etf_rows or []:
        row = pack_row(s, q, f, "ETF")
        if row:
            cands.append(row)
    cands.sort(key=lambda x: (-x["rank_sc"], -(x["chg"] or 0)))
    dragons = []
    per = {}
    for c in cands:
        n = per.get(c["line"], 0)
        if n >= 2:
            continue
        per[c["line"]] = n + 1
        dragons.append(c)
        if len(dragons) >= 8:
            break

    ovn["day_boards"] = day_boards
    ovn["dragons"] = dragons
    if dragons:
        ovn["picks"] = dragons
    elif not ovn.get("picks"):
        ovn["picks"] = []
    return ovn


def flow_sets(inn, outf):
    inn_lines, out_lines = [], []
    for row in inn:
        tok = sector_token(row)
        for k, line in FLOW_LINE:
            if k in tok:
                if line not in inn_lines:
                    inn_lines.append(line)
                break
        else:
            if tok and tok not in inn_lines:
                inn_lines.append(tok)
    for row in outf:
        tok = sector_token(row)
        for k, line in FLOW_LINE:
            if k in tok:
                if line not in out_lines:
                    out_lines.append(line)
                break
        else:
            if tok and tok not in out_lines:
                out_lines.append(tok)
    return inn_lines, out_lines


# 东财流出里常见、但不是自选主线的行业。只记资金流出，不进「回避主线」。
NOISE_OUT = {
    "建筑材料", "建材", "水泥",
    "装修装饰", "房地产开发", "房地产",
}


def desk_lines_of(stocks, etfs=None):
    """自选股票/ETF 对得上的主线名。"""
    lines = []
    for s in stocks or []:
        ln = line_of_board((s or {}).get("board"))
        if ln and ln not in lines:
            lines.append(ln)
    for e in etfs or []:
        name = (e or {}).get("name") or ""
        ln = ETF_LINE.get(name) or line_of_board((e or {}).get("board"))
        if ln and ln not in lines:
            lines.append(ln)
    return lines


def avoid_for_desk(avoid, desk_lines):
    """首页回避主线：只显示自选对口、且该线自己主力净出。
    玻纤/建材等不进闸。亨通跟 CPO，通信线缆流出不显示成光通信回避。"""
    desk = set(desk_lines or [])
    shown = []
    for x in avoid or []:
        if x in NOISE_OUT:
            continue
        if x in desk or x == "电子/科技":
            if x not in shown:
                shown.append(x)
    return shown


def line_policy(inn_lines, out_lines):
    """东财行业主力 → 可做/回避。
    同名板块有进有出：按流入算。回避=该线自己净出，不按涨幅、不连坐兄弟线。
    亨通/光纤按 CPO 光通信；东财通信线缆流出≠光模块冷。元件/PCB 不连坐光通信、半导体、液冷。
    只有大类「电子/科技」净流出，才把未流入的科技链一起回避。"""
    avoid = []
    for x in out_lines:
        if x in inn_lines:
            continue
        if x in NOISE_OUT:
            continue
        # 东财「通信线缆」流出不把 CPO/光模块打成回避；亨通按光通信走。
        if x == "通信线缆" and "光通信" in inn_lines:
            continue
        if x not in avoid:
            avoid.append(x)
    if "电子/科技" in avoid:
        for x in sorted(TECH_LINES):
            if x not in inn_lines and x not in avoid:
                avoid.append(x)
    doable = []
    for x in inn_lines:
        if x not in avoid and x not in doable:
            doable.append(x)
    return doable, avoid


def flow_heat_map(inn, outf):
    """当日板块主力热度。只给排名用，不进表一分、不进7因子。"""
    m = {}

    def eat(rows, side):
        for row in rows:
            tok = sector_token(row)
            line = tok
            for k, ln in FLOW_LINE:
                if k in tok:
                    line = ln
                    break
            cm = re.search(r"([+-]?\d+\.?\d*)%", str(row))
            am = re.search(r"主力([+-]?\d+\.?\d*)亿", str(row))
            chg = float(cm.group(1)) if cm else 0.0
            amt = float(am.group(1)) if am else 0.0
            prev = m.get(line)
            if not prev or abs(amt) > abs(prev["amt"]):
                m[line] = {"amt": amt, "chg": chg, "side": side, "name": tok}

    eat(inn, "in")
    eat(outf, "out")
    return m


def hy_match_board(hy, board):
    hy = hy or ""
    if not board:
        return False
    if board in hy:
        return True
    for k in BOARD_HY_KEYS.get(board) or []:
        if k in hy:
            return True
    return False


ZT_DIR = os.path.join(ROOT, "reports", "ztpool")


def prev_trade_date_str(now=None, hist=None):
    """上一个交易日 YYYYMMDD。优先用已完成日线的最后一根（最准，自带节假日），
    拿不到再按自然日回退并跳过周末。"""
    if hist:
        try:
            return str(hist[-1][0]).replace("-", "")
        except Exception:
            pass
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    d = now - datetime.timedelta(days=1)
    for _ in range(7):
        if d.weekday() < 5:
            return d.strftime("%Y%m%d")
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def zt_pool(date_s=None, pages=6, pagesize=100):
    """东财涨停池。给出连板数 lbc、封单额 fund、开板次数 zbc、首封时间 fbt、换手 hs。
    打板过去缺的就是这几个数，不再靠猜。历史日期落盘缓存，只抓一次。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    date_s = date_s or now.strftime("%Y%m%d")
    today_s = now.strftime("%Y%m%d")
    fn = os.path.join(ZT_DIR, f"{date_s}.json")
    if date_s != today_s and os.path.isfile(fn):
        try:
            return json.load(open(fn, encoding="utf-8"))
        except Exception:
            pass
    rows = []
    for pi in range(pages):
        url = (
            "https://push2ex.eastmoney.com/getTopicZTPool?"
            "ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
            f"&Pageindex={pi}&pagesize={pagesize}&sort=fbt%3Aasc&date={date_s}"
        )
        try:
            j = http_json(url, timeout=8, referer="https://quote.eastmoney.com/")
        except Exception:
            break
        pool = ((j.get("data") or {}) or {}).get("pool") or []
        if not pool:
            break
        for x in pool:
            try:
                rows.append({
                    "code": str(x.get("c") or "").zfill(6),
                    "name": x.get("n") or "",
                    "chg": float(x.get("zdp") or 0),
                    "lbc": int(x.get("lbc") or 1),
                    "fund": float(x.get("fund") or 0),
                    "amount": float(x.get("amount") or 0),
                    "zbc": int(x.get("zbc") or 0),
                    "fbt": int(x.get("fbt") or 0),
                    "hs": float(x.get("hs") or 0),
                    "ltsz": float(x.get("ltsz") or 0),
                    "hy": x.get("hybk") or "",
                })
            except Exception:
                continue
        if len(pool) < pagesize:
            break
    if rows and date_s != today_s:
        try:
            os.makedirs(ZT_DIR, exist_ok=True)
            json.dump(rows, open(fn, "w", encoding="utf-8"), ensure_ascii=False)
        except Exception:
            pass
    return rows


def zt_map(rows):
    return {r["code"]: r for r in rows or []}


def zt_quality(rec):
    """板的质量：封成比=封单额/成交额，开板次数，是否一字/秒板。"""
    if not rec:
        return {"tag": "无记录", "ratio": None, "hard": None, "yizi": False}
    amt = rec.get("amount") or 0
    ratio = (rec.get("fund") or 0) / amt if amt > 0 else None
    zbc = rec.get("zbc") or 0
    fbt = rec.get("fbt") or 0
    hs = rec.get("hs") or 0
    yizi = fbt <= 93100 and hs < 3
    if ratio is None:
        tag, hard = "封单未知", None
    elif zbc >= 2:
        tag, hard = f"烂板(开板{zbc}次)", False
    elif ratio >= 0.5 and zbc == 0:
        tag, hard = f"硬板(封成{ratio:.2f})", True
    elif ratio >= 0.15:
        tag, hard = f"一般板(封成{ratio:.2f}{'/开板'+str(zbc)+'次' if zbc else ''})", zbc == 0
    else:
        tag, hard = f"弱板(封成{ratio:.2f}{'/开板'+str(zbc)+'次' if zbc else ''})", False
    if yizi:
        tag = "一字板/秒板 " + tag
    return {
        "tag": tag, "ratio": ratio, "hard": hard, "yizi": yizi,
        "zbc": zbc, "lbc": rec.get("lbc") or 1, "hs": hs,
        "fund_yi": (rec.get("fund") or 0) / 1e8,
    }


def zt_ladder(rows):
    """连板梯队。最高板和各高度家数，用来判空间压制和情绪位置。"""
    lad = {}
    for r in rows or []:
        lad[r["lbc"]] = lad.get(r["lbc"], 0) + 1
    hi = max(lad) if lad else 0
    return {"ladder": lad, "high": hi, "n": len(rows or [])}


def daban_env(zt_yday_rows, live_map=None):
    """赚钱效应：昨日涨停股今天的平均涨幅、晋级率、翻绿率。
    这是打板值不值得做的直接证据，比涨停家数有用。"""
    rows = zt_yday_rows or []
    out = {
        "n": 0, "prem": None, "adv": None, "green": None,
        "txt": "昨板今日表现暂缺", "ok": None,
    }
    if not rows:
        return out
    codes = [("sh" if r["code"].startswith(("6", "9")) else "sz") + r["code"] for r in rows[:120]]
    live = live_map or {}
    need = [c for c in codes if c[2:] not in live]
    if need:
        for i in range(0, len(need), 60):
            try:
                live.update(tencent(need[i:i + 60]))
            except Exception:
                continue
    chgs, adv, green = [], 0, 0
    for r in rows[:120]:
        q = live.get(r["code"])
        if not q:
            continue
        chgs.append(q["chg"])
        if is_limit_up(r["code"], q["chg"], q.get("name")):
            adv += 1
        if q["chg"] < 0:
            green += 1
    if not chgs:
        return out
    out["n"] = len(chgs)
    out["prem"] = sum(chgs) / len(chgs)
    out["adv"] = adv / len(chgs) * 100
    out["green"] = green / len(chgs) * 100
    out["ok"] = out["prem"] >= 0.5 and out["adv"] >= 10
    out["txt"] = (
        f"昨板{out['n']}只今日均{out['prem']:+.2f}%、晋级{out['adv']:.0f}%、翻绿{out['green']:.0f}%"
        + ("，赚钱效应在" if out["ok"] else "，赚钱效应差，打板降级")
    )
    return out


def market_mood(zt_rows=None, env=None):
    """全市场涨停生态。只作表二环境层，不进 7 因子分。"""
    empty = {
        "phase": "不明", "n_zt": 0, "n_dt": 0, "n_20": 0,
        "note": "涨停统计暂缺", "by_board": {}, "txt": "情绪：暂缺",
    }

    def clist(pn, po, pz=100):
        fs = "m:0+t:6+f:!2,m:0+t:80+f:!2,m:1+t:2+f:!2,m:1+t:23+f:!2"
        fields = "f12,f14,f3,f100,f8"
        urls = [
            f"https://push2delay.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}",
            f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}",
        ]
        for url in urls:
            try:
                d = http(url, timeout=8)
                diff = ((d.get("data") or {}).get("diff") or [])
                if isinstance(diff, dict):
                    diff = list(diff.values())
                if diff:
                    return diff
            except Exception:
                continue
        return []

    zt, dt = [], []
    for pn in range(1, 6):
        diff = clist(pn, 1)
        if not diff:
            break
        page_zt = 0
        page_max = -999
        for x in diff:
            try:
                code = str(x["f12"]).zfill(6)
                name = str(x.get("f14") or "")
                chg = float(x.get("f3") or 0)
            except Exception:
                continue
            page_max = max(page_max, chg)
            if name.startswith("N") or "ST" in name:
                continue
            if is_limit_up(code, chg, name):
                zt.append({"code": code, "name": name, "chg": chg, "hy": str(x.get("f100") or "")})
                page_zt += 1
        if page_zt == 0 and page_max < 9.5:
            break
    for pn in range(1, 3):
        diff = clist(pn, 0)
        if not diff:
            break
        page_dt = 0
        page_min = 999
        for x in diff:
            try:
                code = str(x["f12"]).zfill(6)
                name = str(x.get("f14") or "")
                chg = float(x.get("f3") or 0)
            except Exception:
                continue
            page_min = min(page_min, chg)
            if "ST" in name:
                continue
            if is_limit_down(code, chg, name):
                dt.append({"code": code, "name": name, "chg": chg, "hy": str(x.get("f100") or "")})
                page_dt += 1
        if page_dt == 0 and page_min > -9.5:
            break

    # 涨停池口径更准（含连板数），有就用它覆盖家数
    pool = zt_rows or []
    if pool:
        zt = [{"code": r["code"], "name": r["name"], "chg": r["chg"], "hy": r["hy"]} for r in pool]
    n_zt, n_dt = len(zt), len(dt)
    n_20 = sum(1 for x in zt if x["code"].startswith(("300", "301", "688")))
    by_board = {}
    for board in BOARD_HY_KEYS:
        names = [x for x in zt if hy_match_board(x["hy"], board)]
        hi = max((x["chg"] for x in names), default=0)
        by_board[board] = (len(names), hi)
    lad = zt_ladder(pool)
    n_lian = sum(v for k, v in (lad.get("ladder") or {}).items() if k >= 2)
    prem = (env or {}).get("prem")
    adv = (env or {}).get("adv")

    # 情绪分档：家数 + 连板梯队 + 昨板赚钱效应。之前打板分里引用的「发酵/启动」是死档，现在补上
    if prem is not None and prem <= -1.5 and n_zt < 60:
        phase, note = "退潮", "昨板today亏钱，打板空仓、游资只看"
    elif n_zt >= 80 and (lad.get("high") or 0) >= 5:
        phase, note = "高潮", "跟风可看，不追高标、不接最高板"
    elif n_zt >= 45 and n_lian >= 6 and (prem is None or prem >= 0):
        phase, note = "发酵", "梯队在长，首板/一进二是主战场"
    elif n_zt < 20 or n_dt >= max(15, n_zt):
        phase, note = "退潮", "游资只看不追，不做首板高潮假设"
    elif n_zt < 40:
        phase, note = "修复", "情绪一般，只做低位转强"
    else:
        phase, note = "平衡", "正常游资环境"
    note += "；不进个股因子分"
    txt = (
        f"情绪{phase}：涨停{n_zt} 跌停{n_dt} 20cm{n_20} 连板{n_lian} 最高{lad.get('high') or 0}板。"
        + (f"{(env or {}).get('txt')}。" if (env or {}).get("prem") is not None else "")
        + note
    )
    return {
        "phase": phase, "n_zt": n_zt, "n_dt": n_dt, "n_20": n_20,
        "note": note, "by_board": by_board, "txt": txt,
        "ladder": lad.get("ladder") or {}, "high": lad.get("high") or 0,
        "n_lian": n_lian, "env": env or {},
    }


def lhb_warn_map():
    """昨龙虎榜净卖出警示。只标注，不进 7 因子。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    out = {}
    for i in range(1, 6):
        d = (now - datetime.timedelta(days=i)).strftime("%Y-%m-%d")
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "sortColumns=BILLBOARD_NET_AMT&sortTypes=1&pageSize=200&pageNumber=1"
            "&reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL&source=WEB&client=WEB"
            f"&filter=(TRADE_DATE%3D'{d}')"
        )
        try:
            j = http(url, timeout=8)
        except Exception:
            continue
        rows = ((j.get("result") or {}).get("data") or [])
        if not rows:
            continue
        for x in rows:
            code = str(x.get("SECURITY_CODE") or "").zfill(6)
            net = x.get("BILLBOARD_NET_AMT")
            try:
                net = float(net)
            except Exception:
                continue
            if net < 0:
                out[code] = f"昨龙虎净卖{net/1e8:.1f}亿"
            elif code not in out:
                out[code] = f"昨龙虎净买{net/1e8:.1f}亿"
        if out:
            break
    return out


def youzi_annot(s, q, f, hist, flow, mood, lhb):
    """表二加列：换手/竞价量/弱转强/超大单/题材板/龙虎 + 连板/昨ZT/板内/竞价额/换手市值。不改 7 因子分。"""
    hs = q.get("turnover") if q else None
    dd = f["dd"] if f else None
    if hs is None:
        hs_txt = "换手-"
    elif dd is not None and dd > -0.05 and hs >= 12:
        hs_txt = f"换手{hs:.1f}%爆量"
    elif dd is not None and dd <= -0.12 and 2 <= hs <= 10:
        hs_txt = f"换手{hs:.1f}%温和"
    elif hs < 1.5:
        hs_txt = f"换手{hs:.1f}%清淡"
    else:
        hs_txt = f"换手{hs:.1f}%"

    o, p, prev = q["open"], q["px"], q["prev"]
    gap = (o / prev - 1) * 100 if prev else 0
    vr = q.get("vol_ratio") or 0
    if gap >= 0.8 and vr < 1.0:
        auc_vol = "竞价量虚"
    elif gap >= 0.5 and vr >= 1.5:
        auc_vol = "竞价量实"
    elif gap <= -0.3 and vr >= 1.2:
        auc_vol = "低开放量"
    else:
        auc_vol = "竞价量一般"

    weak_yest = False
    done = strip_today(hist) if hist else []
    if done and len(done) >= 2:
        weak_yest = done[-1][4] < done[-2][4]
    w2s = "弱转强" if weak_yest and q["chg"] > 0.3 else "否"

    xlarge = (flow or {}).get("xlarge") or 0
    main = (flow or {}).get("main") or 0
    if xlarge > 0 and main < 0:
        xl = "超大进主力出"
    elif abs(xlarge) >= 1e7:
        xl = f"超大{xlarge/1e8:+.1f}亿"
    else:
        xl = "超大平"

    nzt, high = (mood.get("by_board") or {}).get(s.get("board") or "", (0, 0))
    theme = f"题材{nzt}板 高{high:.0f}%" if nzt else "题材0板"
    lhb_txt = (lhb or {}).get(s["code"]) or "未上榜"

    n_lian = consec_zt(s["code"], hist)
    if n_lian >= 2:
        lian = f"{n_lian}连板"
    elif n_lian == 1:
        lian = "昨首板"
    else:
        lian = "非连板"
    n7 = recent_zt(s, hist, n=7)
    if n7 >= 3:
        zt7 = f"近7日{n7}次涨停·打板过热"
    elif n7 >= 2:
        zt7 = f"近7日{n7}次涨停"
    elif n7 == 1:
        zt7 = "近7日1板"
    else:
        zt7 = "近7日无板"
    if yday_zt(s["code"], hist):
        zt_prem = f"昨涨停今{q['chg']:+.1f}%"
    else:
        zt_prem = "非昨涨停"

    yi = mcap_yi(q)
    if yi is not None and yi <= 220 and hs is not None and hs < 5:
        hs_fit = "小票换手不足"
    elif yi is not None and yi >= 500 and vr < 1.2:
        hs_fit = "大票弹性虚"
    else:
        hs_fit = "换手匹配"

    minutes = (f or {}).get("_min") or []
    auc_amt = auc_amt_txt(minutes, hist)
    return {
        "hs_txt": hs_txt, "auc_vol": auc_vol, "w2s": w2s,
        "xl": xl, "theme": theme, "lhb": lhb_txt,
        "lian": lian, "zt_prem": zt_prem, "hs_fit": hs_fit,
        "auc_amt": auc_amt, "board_rank": (f or {}).get("board_rank") or "-",
        "zt7": zt7, "n7": n7, "n_lian": n_lian,
    }


def index_left_weak(live, idx_hist):
    """大盘环境：只压表三趋势可试仓，不改表一表二。"""
    weak, reasons = False, []
    for code, name in (("000001", "上证"), ("399006", "创业板")):
        q = live.get(code)
        hist = idx_hist.get(code) or []
        if q:
            shp = index_shape(q)
            if shp in ("高开低走", "低开冲高回落", "开后走弱"):
                weak = True
                reasons.append(f"{name}{shp}")
        if q and hist:
            ma20 = sma([b[4] for b in hist], 20)
            if ma20 and q["px"] < ma20:
                weak = True
                reasons.append(f"{name}在MA20下")
    return weak, reasons


def lower_shadow(q):
    rng = (q["high"] - q["low"]) if q else 0
    if rng <= 0:
        return 0.0
    return (min(q["open"], q["px"]) - q["low"]) / rng


def consecutive_down(hist):
    n = 0
    if not hist or len(hist) < 2:
        return 0
    for i in range(len(hist) - 1, 0, -1):
        if hist[i][4] < hist[i - 1][4]:
            n += 1
        else:
            break
    return n


def fib_retracement(hist, q):
    """20日波段前高→前低回撤。0=仍在前高，1=打到前低。只标注，不参与打分/买卖。"""
    bars = list(hist[-20:]) if hist else []
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    if q:
        highs.append(q["high"])
        lows.append(q["low"])
    if not highs or not lows:
        return "-"
    hi, lo = max(highs), min(lows)
    if hi <= lo or not q:
        return "-"
    r = (hi - q["px"]) / (hi - lo)
    pct = r * 100
    levels = [(0.236, "23.6"), (0.382, "38.2"), (0.5, "50"), (0.618, "61.8"), (0.786, "78.6")]
    near_lvl, near_name = min(levels, key=lambda x: abs(r - x[0]))
    if r < 0.12:
        return f"回撤{pct:.0f}% 仍近前高"
    if r > 0.92:
        return f"回撤{pct:.0f}% 近前低"
    if abs(r - near_lvl) <= 0.07:
        return f"回撤{pct:.0f}% 近{near_name}"
    return f"回撤{pct:.0f}%"


def left_setup(s, q, f, yld, hist, flow, idx_weak=False):
    """左侧超跌：位置低 + 止跌确认。不改表一/表二。loc/conf/左侧分公式不动；可试仓多确认。"""
    kind = stock_kind(s, q, hist)
    px = q["px"]
    ma20 = f["ma20"] if f else None
    rsi_v = f["rsi"] if f else None
    dd = f["dd"] if f else None
    bias = ((px / ma20) - 1) * 100 if ma20 else 0.0
    vr = q.get("vol_ratio") or 0
    shad = lower_shadow(q)
    downs = consecutive_down(hist)
    main = (flow or {}).get("main") or 0
    vwap = q.get("vwap")
    above_vwap = vwap is not None and px >= vwap
    green = q["chg"] > 0 or px >= q["open"]
    o, p, prev = q["open"], q["px"], q["prev"]
    gap = (o / prev - 1) * 100 if prev else 0

    # 超跌要多信号共振。原来是「任意一条成立」就进池，单一个 RSI 38 就能把
    # 下跌中继当成左侧机会；现在要 2 条以上同时成立。
    os_bits = []
    if ma20 is not None and px < ma20 and bias <= -5:
        os_bits.append("破MA20且乖离")
    if bias <= -8:
        os_bits.append("乖离深")
    if dd is not None and dd <= -0.15:
        os_bits.append("距高远")
    if rsi_v is not None and rsi_v <= 38:
        os_bits.append("RSI低")
    if yld:
        os_bits.append("昨跌停")
    if downs >= 4:
        os_bits.append("连阴")
    if len(os_bits) < 2:
        return None

    loc = 0
    if bias <= -15:
        loc += 40
    elif bias <= -8:
        loc += 28
    elif ma20 and px < ma20:
        loc += 12
    if dd is not None:
        if dd <= -0.25:
            loc += 30
        elif dd <= -0.15:
            loc += 20
        elif dd <= -0.10:
            loc += 10
    if rsi_v is not None:
        if rsi_v <= 25:
            loc += 25
        elif rsi_v <= 35:
            loc += 18
        elif rsi_v <= 42:
            loc += 10
    if downs >= 3:
        loc += 10
    loc = clip(loc, 0, 100)

    conf = 40
    bits = []
    if green:
        conf += 18
        bits.append("收阳")
    if shad >= 0.35:
        conf += 16
        bits.append("长下影")
    if above_vwap:
        conf += 16
        bits.append("站回均价")
    if vr >= 1.2:
        conf += 10
        bits.append("放量")
    if main > 0:
        conf += 12
        bits.append("今主力进")
    if yld:
        conf -= 20
        bits.append("昨跌停")
    if yld and gap >= 0.8 and p < o:
        conf -= 28
        bits.append("骗炮")
    elif gap <= -0.3 and q["chg"] > 0:
        conf += 12
        bits.append("低开翻红")
    if is_limit_up(s["code"], q["chg"]) or q["chg"] >= 7:
        conf -= 25
        bits.append("反弹过热")
    conf = clip(conf, 0, 100)
    score = 0.55 * loc + 0.45 * conf

    pos = f"乖离{bias:+.1f}% RSI{rsi_v:.0f}" if rsi_v is not None else f"乖离{bias:+.1f}%"
    if dd is not None:
        pos += f" 距高{dd*100:.0f}%"
    factor = "；".join(bits) if bits else "未止跌"

    slope = ma_slope_pct([b[4] for b in hist], 20, 5) if hist else None
    ma20_diving = slope is not None and slope <= -0.8
    rsi_ok = rsi_turn_up(hist, q, f)
    vol_ok = vol_dry_then_expand(hist, q)
    dbl_ok = double_bottom(hist, q, green)
    confirm = []
    if slope is None:
        confirm.append("斜率缺")
    elif ma20_diving:
        confirm.append("均线加速跌")
    elif slope >= 0.4:
        confirm.append("均线走平转升")
    else:
        confirm.append("均线走平")
    if rsi_ok:
        confirm.append("RSI拐头")
    if vol_ok:
        confirm.append("缩量后再放量")
    if dbl_ok:
        confirm.append("二探不破")
    confirm_txt = "；".join(confirm) if confirm else "-"

    atr = atr14(hist)
    fail = f"破今日低{q['low']:.2f}走"
    if atr:
        fail += f" / ATR{px - atr:.2f}"

    if overheat(s["code"], q["chg"], q.get("name")) in ("涨停", "见顶", "不追"):
        call, how = "不抄", "反弹过热，左侧不做"
    elif yld and gap >= 0.8 and p < o:
        call, how = "不抄", "昨跌停骗炮"
    elif q["chg"] < 0 and not above_vwap and shad < 0.30:
        call, how = "观察", "位置超跌，还在阴跌未止跌"
    elif kind == "游资":
        if green and above_vwap and loc >= 40 and q["chg"] < 5:
            call, how = "可试仓", "游资超跌翻红，快进快出，" + fail
        else:
            call, how = "观察", "游资超跌，等翻红并站稳均价"
    else:
        if above_vwap and green and loc >= 45 and q["chg"] < 3:
            call, how = "可试仓", "趋势超跌止跌，轻仓试，" + fail
        else:
            call, how = "观察", "趋势超跌，等站回均价再试"

    # 止跌确认对两条战法同一个标准。原来游资左侧只要翻红就能试仓，
    # 趋势左侧却要 RSI 拐头/缩量再放量/二探，同一个「左侧超跌」两套安全线。
    if call == "可试仓":
        if ma20_diving:
            call, how = "观察", "均线还在加速下跌，超跌钝化，只盯不抄"
        elif idx_weak:
            call, how = "观察", "大盘偏弱，左侧只盯不抄"
        elif not (rsi_ok or vol_ok or dbl_ok):
            call, how = "观察", "缺RSI拐头/缩量再放量/二探，未确认止跌"

    return {
        "score": score, "kind": kind, "call": call, "how": how,
        "loc": loc, "conf": conf, "bias": bias, "pos": pos, "factor": factor,
        "rsi": rsi_v, "dd": dd, "main": main, "fib": fib_retracement(hist, q),
        "confirm": confirm_txt, "fail": fail, "oversold_bits": os_bits,
    }


# ---------- 判别滞后带：临界值升级要连续两次确认，降级立即生效 ----------
GATE_PATH = os.path.join(ROOT, "reports", "gate_state.json")
GATE_PREV = {}
GATE_NOW = {}


def gate_state_load():
    global GATE_PREV
    try:
        blob = json.load(open(GATE_PATH, encoding="utf-8"))
        today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        GATE_PREV = blob.get("codes") or {} if blob.get("date") == today else {}
    except Exception:
        GATE_PREV = {}


def gate_state_save():
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        os.makedirs(os.path.dirname(GATE_PATH), exist_ok=True)
        json.dump(
            {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M"), "codes": GATE_NOW},
            open(GATE_PATH, "w", encoding="utf-8"), ensure_ascii=False,
        )
    except Exception:
        pass


SOFT_DOWNGRADE = ("未达标", "无量站上均价", "量比过低且收阴")


def hysteresis(code, call, why, score=None, enter=None, hold=None, soft=SOFT_DOWNGRADE):
    """同一天里同一只票在阈值上反复翻转最伤胜率。
    升级到可小仓：分数刚过线（enter~enter+5）要连续两次达标才放行；
    已在可小仓：分数回落到 hold 以上、且降级理由只是「分数/量比没够」这类阈值噪声时，
    维持可小仓不来回改口。
    回避、退潮、骗炮、破均价、涨停过热这些硬否决不在 soft 里，立即生效不等确认。"""
    prev = GATE_PREV.get(code) or {}
    marginal = (
        call == "可小仓" and score is not None and enter is not None and score < enter + 5
    )
    out_call, out_why = call, why
    if marginal and not (prev.get("call") == "可小仓" or prev.get("marginal")):
        out_call = "观察"
        out_why = (why or "") + f"；{score:.0f}分刚过线，等下一次确认再动手"
    elif (
        call == "观察" and prev.get("call") == "可小仓"
        and score is not None and hold is not None and score >= hold
        and any(k in (why or "") for k in soft)
    ):
        out_call = "可小仓"
        out_why = f"在滞后带内（{score:.0f}≥{hold:.0f}），维持可小仓不来回改口（原因：{why}）"
    GATE_NOW[code] = {"call": out_call, "raw": call, "marginal": bool(marginal), "score": round(score or 0, 1)}
    return out_call, out_why


# ---------- 信号留档与复盘：闭环的那一环 ----------
JOURNAL_DIR = os.path.join(ROOT, "reports", "journal")


def _journal_file(date_s):
    return os.path.join(JOURNAL_DIR, date_s[:7] + ".jsonl")


def journal_load(months=3):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    recs = []
    seen_files = set()
    for i in range(months):
        m = (now - datetime.timedelta(days=31 * i)).strftime("%Y-%m")
        fn = _journal_file(m + "-01")
        if fn in seen_files or not os.path.isfile(fn):
            continue
        seen_files.add(fn)
        for ln in open(fn, encoding="utf-8"):
            ln = ln.strip()
            if not ln:
                continue
            try:
                recs.append(json.loads(ln))
            except Exception:
                continue
    return recs


def journal_record(rows, now):
    """每次跑把判别留档。同一天同一只票同一结论只记一次，结论变了再记一条。"""
    date_s, time_s = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    old = journal_load(1)
    have = {(r.get("date"), r.get("code"), r.get("call")) for r in old}
    new = []
    for r in rows:
        key = (date_s, r.get("code"), r.get("call"))
        if key in have:
            continue
        have.add(key)
        new.append({
            "date": date_s, "time": time_s, "code": r.get("code"), "name": r.get("name"),
            "kind": r.get("kind"), "call": r.get("call"), "line": r.get("line"),
            "st": r.get("st"), "worth": round(r.get("score") or 0, 1),
            "tape": round(r.get("tape") or 0, 1), "ma": round(r.get("ma") or 0, 1),
            "px": r.get("px"), "chg": r.get("chg"), "auc": r.get("auc"),
        })
    if not new:
        return 0
    try:
        os.makedirs(JOURNAL_DIR, exist_ok=True)
        with open(_journal_file(date_s), "a", encoding="utf-8") as fh:
            for r in new:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except Exception:
        return 0
    return len(new)


def _fwd_from(hist, date_s, px):
    """信号当时的价 → 之后第1/3/5个交易日收盘。用已经抓下来的日线，不额外请求。"""
    if not hist or not px:
        return {}
    idx = None
    for i, b in enumerate(hist):
        if b[0] > date_s:
            idx = i
            break
    if idx is None:
        return {}
    out = {}
    for tag, step in (("r1", 0), ("r3", 2), ("r5", 4)):
        j = idx + step
        if j < len(hist):
            out[tag] = (hist[j][4] / px - 1) * 100
    return out


def journal_review(hist_by_code, days=30, cost_pct=0.1):
    """按判别分桶算胜率和平均收益。这张表是用来改阈值的依据，不参与今天的闸。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    recs = [r for r in journal_load(3) if since <= (r.get("date") or "") < today]
    buckets = {}
    per_kind = {}
    n_eval = 0
    for r in recs:
        hist = hist_by_code.get(r.get("code"))
        if not hist:
            continue
        fwd = _fwd_from(hist, r["date"], r.get("px"))
        if "r1" not in fwd:
            continue
        n_eval += 1
        net1 = fwd["r1"] - cost_pct
        for key, box in ((r.get("call") or "-", buckets), ((r.get("call") or "-") + "/" + (r.get("kind") or "-"), per_kind)):
            b = box.setdefault(key, {"n": 0, "win": 0, "s1": 0.0, "s3": 0.0, "s5": 0.0, "n3": 0, "n5": 0})
            b["n"] += 1
            b["win"] += 1 if net1 > 0 else 0
            b["s1"] += net1
            if "r3" in fwd:
                b["s3"] += fwd["r3"] - cost_pct
                b["n3"] += 1
            if "r5" in fwd:
                b["s5"] += fwd["r5"] - cost_pct
                b["n5"] += 1

    def pack(box):
        rows = []
        for k, b in box.items():
            if not b["n"]:
                continue
            rows.append({
                "key": k, "n": b["n"], "win": b["win"] / b["n"] * 100,
                "a1": b["s1"] / b["n"],
                "a3": (b["s3"] / b["n3"]) if b["n3"] else None,
                "a5": (b["s5"] / b["n5"]) if b["n5"] else None,
            })
        order = {"可小仓": 0, "可试仓": 1, "观察": 2, "不追": 3, "不买": 4}
        rows.sort(key=lambda x: (order.get(x["key"].split("/")[0], 9), -x["n"]))
        return rows

    return {
        "days": days, "n_rec": len(recs), "n_eval": n_eval,
        "by_call": pack(buckets), "by_kind": pack(per_kind), "cost_pct": cost_pct,
    }


# ---------- 组合层：主线限仓 + 按止损距离反算仓位 ----------
RISK_PATH = os.path.join(ROOT, "risk_config.json")
RISK_DEFAULT = {
    "capital": 100000,
    "risk_pct": 1.0,        # 单笔愿意亏掉总资金的百分比
    "max_total_pct": 60,    # 总仓上限
    "max_line_pct": 25,     # 单条主线仓位上限
    "max_name_pct": 20,     # 单票仓位上限
    "max_per_line": 2,      # 同一条主线最多几只
    "cost_pct": 0.1,        # 双边交易成本（印花税+佣金+过户费）
}


def load_risk_cfg():
    cfg = dict(RISK_DEFAULT)
    try:
        cfg.update(json.load(open(RISK_PATH, encoding="utf-8")) or {})
    except Exception:
        pass
    return cfg


def portfolio_plan(cands, exits_by_code, cfg):
    """把「可以买」变成「买多少」。
    仓位 = 单笔风险额 ÷ 止损距离，再被单票/单主线/总仓三道上限压住；
    同一条主线最多 max_per_line 只，避免 4 个名字其实是同一个赌注。"""
    cap = float(cfg.get("capital") or 0)
    risk_amt = cap * float(cfg.get("risk_pct") or 0) / 100.0
    max_total = cap * float(cfg.get("max_total_pct") or 100) / 100.0
    max_line = cap * float(cfg.get("max_line_pct") or 100) / 100.0
    max_name = cap * float(cfg.get("max_name_pct") or 100) / 100.0
    per_line = int(cfg.get("max_per_line") or 99)
    cost = float(cfg.get("cost_pct") or 0)
    plan, skipped = [], []
    used_total = 0.0
    used_line = {}
    cnt_line = {}
    for c in cands:
        code, line, px = c.get("code"), c.get("line") or "其他", c.get("px") or 0
        ex = exits_by_code.get(code) or {}
        sl = ex.get("sl")
        if not px or not sl or sl >= px:
            skipped.append({**c, "reason": "没有有效止损位，不给仓位"})
            continue
        if cnt_line.get(line, 0) >= per_line:
            skipped.append({**c, "reason": f"{line}已占满{per_line}只，同线不再加"})
            continue
        stop_dist = px - sl
        tp1 = ex.get("tp1")
        net_rr = None
        if tp1 and tp1 > px:
            net_rr = (tp1 - px - px * cost / 100) / (stop_dist + px * cost / 100)
        # 扣掉成本后盈亏比太差就不该给仓位。这种多半是没有有效结构位、
        # 止损只能用固定百分比顶上，说明当前位置本身不好，不是仓位问题。
        if net_rr is not None and net_rr < 1.2:
            skipped.append({**c, "reason": f"净盈亏比仅{net_rr:.1f}（<1.2），位置不好不给仓位"})
            continue
        want = risk_amt / stop_dist * px if stop_dist > 0 else 0
        room_name = max_name
        room_line = max_line - used_line.get(line, 0)
        room_total = max_total - used_total
        amt = min(want, room_name, room_line, room_total)
        if amt <= 0 or room_line <= 0:
            skipped.append({**c, "reason": f"{line}主线敞口已满" if room_line <= 0 else "总仓已满"})
            continue
        shares = int(amt / px / 100) * 100
        if shares < 100:
            skipped.append({
                **c,
                "reason": f"1手要{px*100:.0f}元，按{cfg.get('risk_pct')}%风险只放得下{amt:.0f}元，装不下1手",
            })
            continue
        real = shares * px
        used_total += real
        used_line[line] = used_line.get(line, 0) + real
        cnt_line[line] = cnt_line.get(line, 0) + 1
        binding = "风险额"
        if amt >= room_total - 1:
            binding = "总仓上限"
        elif amt >= room_line - 1:
            binding = f"{line}主线上限"
        elif amt >= room_name - 1:
            binding = "单票上限"
        plan.append({
            **c,
            "shares": shares, "amt": real, "pct": real / cap * 100 if cap else 0,
            "sl": sl, "tp1": tp1, "stop_pct": (sl / px - 1) * 100,
            "risk_amt": shares * stop_dist, "risk_pct_real": shares * stop_dist / cap * 100 if cap else 0,
            "net_rr": net_rr, "binding": binding,
        })
    line_share = {
        k: v / used_total * 100 for k, v in used_line.items()
    } if used_total else {}
    warn = []
    for k, v in sorted(line_share.items(), key=lambda x: -x[1]):
        if v >= 60 and len(line_share) > 1:
            warn.append(f"{k}占计划仓位{v:.0f}%，这不是{len(plan)}个标的，是1个赌注")
    # 过闸名单本身的集中度。计划仓位可能因资金/盈亏比被挡住而看不出来，
    # 但「可以买」列了一串同主线的票，本质还是一个方向的重复下注。
    cand_line = {}
    for c in cands:
        if c.get("call") == "可小仓":
            cand_line[c.get("line") or "其他"] = cand_line.get(c.get("line") or "其他", 0) + 1
    n_cand = sum(cand_line.values())
    if n_cand >= 3:
        for k, v in sorted(cand_line.items(), key=lambda x: -x[1]):
            if v / n_cand >= 0.5 and v >= 2:
                warn.append(
                    f"过闸的 {n_cand} 只里有 {v} 只是{k}，同一个方向。真要做也只当 1 笔，"
                    f"按同主线最多{per_line}只执行"
                )
                break
    return {
        "plan": plan, "skipped": skipped, "used_total": used_total,
        "used_pct": used_total / cap * 100 if cap else 0,
        "line_share": line_share, "warn": warn, "cfg": cfg,
        "risk_amt": risk_amt,
    }


def name_call(s, q, f, yld):
    """趋势闸。保留：今涨停不追、昨跌停骗炮、MA20、RSI≥70。改：量能否决、今涨跟ATR/板、均价允许站回。"""
    dt = yday_dt_shape(q, yld)
    extra = ""
    if dt == "trap":
        return "不买", "昨跌停骗炮（冲高回落/高开低走）"
    if dt == "turn":
        extra = "；昨跌停弱转强，仓更小"
    elif dt == "weak":
        extra = "；昨跌停次日偏弱"
    if is_limit_up(s["code"], q["chg"], q.get("name")):
        return "不追", "今涨停，空间见顶"
    if limit_open_dump(s, q):
        return "不买", "竞价涨停开后砸盘，出货不做"
    vwap = q.get("vwap")
    px = q["px"]
    f = f or {}
    below = vwap is not None and px < vwap
    ma20 = f.get("ma20")
    above_ma20 = bool(ma20) and px > ma20
    hot_rsi = f.get("rsi") is not None and f["rsi"] >= 70
    vr = vr_norm(q.get("vol_ratio") or 0)
    red = q["chg"] < 0 or px < q["open"]
    vp = f.get("vp") or ""
    cap = trend_chg_cap(s["code"], f.get("atr_pct"))
    reclaim = bool(f.get("vwap_reclaim"))
    held = f.get("vwap_held", True)
    vwap_ok = (not below) and ((not reclaim) or held)

    if below and q["chg"] < 0:
        return "不买", "阴跌破分时均价" + extra
    if below:
        if above_ma20:
            return "观察", "还在均价下，未收回" + extra
        return "不买", "均线偏弱" + extra
    if reclaim and not held:
        if above_ma20:
            return "观察", "收回均价但破当日关键低" + extra
        return "不买", "均线偏弱；收回均价但破当日关键低" + extra

    if above_ma20 and vwap_ok and q["chg"] < cap and not hot_rsi:
        why_ok = ("站回均价且未破关键低、涨幅未过热" if reclaim else "均价上、涨幅未过热、均线未坏") + extra
        if vr < 0.8 and red:
            return "观察", "量比过低且收阴，不能小仓" + extra
        if "价涨资金出" in vp:
            return "观察", "价涨资金出，不能小仓" + extra
        if vr < 1.0:
            return "观察", "无量站上均价，胜率差" + extra
        return "可小仓", why_ok
    if above_ma20:
        if hot_rsi or q["chg"] >= cap:
            return "观察", f"站上20日线，今涨过热（上限{cap:.1f}%/ATR·板）或RSI高，等回踩" + extra
        return "观察", "站上20日线，等回踩均价确认" + extra
    return "不买", "均线偏弱" + extra


SKIP_HOW = ("骗炮", "空间见顶", "涨停，", "结束/观察")


def verdict_trend(s, q, f, yld, st):
    """趋势仓最终买点。不改 name_call。过线后加滞后带，避免同日反复改口。"""
    call, why = name_call(s, q, f, yld)
    if call == "可小仓" and st == "回避":
        return "观察", "主线回避"
    ent = (f or {}).get("entry")
    if call in ("可小仓", "观察") and ent:
        return hysteresis(s["code"], call, why, ent, 55, 48)
    return call, why


def verdict_youzi(s, q, f, yld, yz, st, mood, late):
    """游资仓最终买点。和总判同一套闸，表一看这一列就能下结论。
    换手/量比改用时段归一值：10:00 的 5% 换手按全天折算才和 14:30 的 5% 可比。"""
    how = (yz or {}).get("how") or ""
    sc = (yz or {}).get("score") or 0
    if yday_dt_shape(q, yld) == "trap":
        return "不买", "昨跌停骗炮"
    oh = overheat(s["code"], q["chg"], q.get("name"))
    if oh in ("涨停", "见顶", "不追"):
        return "不追", f"今涨{q['chg']:+.1f}% {oh}，不追"
    if limit_open_dump(s, q):
        return "不买", "竞价涨停开后砸盘，出货不做"
    if mood.get("phase") == "退潮":
        return "观察", "情绪退潮"
    if any(k in how for k in SKIP_HOW):
        return "不买", how
    if sc < 65:
        return hysteresis(s["code"], "观察", f"7a {sc:.0f}未达标", sc, 65, 60)
    if st == "回避":
        return "观察", "主线回避"
    hs_raw = q.get("turnover")
    hs = hs_proj(hs_raw)
    vr = vr_norm(q.get("vol_ratio") or 0)
    # 折算值要过 5%，同时已成交的换手本身不能太小，否则等于拿开盘几分钟的噪声开仓
    hs_ok = hs is not None and hs >= 5 and (hs_raw or 0) >= 1.0
    if not (hs_ok or vr >= 1.5):
        if hs is not None:
            return "观察", f"换手/量比不够（现换手{hs_raw:.1f}%、全天折算{hs:.1f}%、归一量比{vr:.2f}）"
        return "观察", "换手/量比不够"
    if late:
        return "观察", "尾盘/收盘后不新开"
    return hysteresis(s["code"], "可小仓", how, sc, 65, 60)


def limit_price(prev, code, name=None):
    if not prev:
        return None
    return round(prev * (1 + board_limit_pct(code, name) / 100.0) + 1e-8, 2)


def daban_plan(s, q, f, hist, yz, st, line, mood, late=False, yld=False,
               zt_y=None, zt_t=None):
    """近7日打板战法。游资习惯：主线+换手板+一进二/弱转强，不打今涨停、一字、退潮、回避。
    本轮改动：昨板质量（封单额/封成比/开板次数/是否一字）从东财涨停池取真值，
    一进二不再只看「昨涨停+今高开」；龙回头要求真的是板内龙头且缩量回踩均线；
    弱转强要求昨天确实是烂板/炸板/断板。不改 7 因子公式；游资仓仍要 7a≥65。"""
    empty = {
        "in_pool": False, "score": 0, "setup": "非打板池", "call": "观察",
        "why": "近7日无涨停", "bits": [], "n7": 0, "n_lian": 0,
    }
    if not s or not q:
        return empty
    n7 = recent_zt(s, hist, n=7)
    n_lian = consec_zt(s["code"], hist)
    yday = yday_zt(s["code"], hist)
    today_zt = is_limit_up(s["code"], q["chg"], q.get("name"))
    yq = zt_quality((zt_y or {}).get(s["code"]))
    tq = zt_quality((zt_t or {}).get(s["code"])) if today_zt else None
    if (zt_y or {}).get(s["code"]):
        yday = True
        n_lian = max(n_lian, yq.get("lbc") or 1)
    if n7 < 1 and n_lian < 1 and not yday and not today_zt:
        return empty
    chg = q["chg"]
    prev, o, p = q.get("prev") or 0, q.get("open") or 0, q["px"]
    gap = (o / prev - 1) * 100 if prev else 0
    hs = q.get("turnover")
    vr = q.get("vol_ratio") or 0
    yi = mcap_yi(q)
    auc = (f or {}).get("auction") or {}
    auc_vr = auc.get("vr")
    auc_call = auc.get("call") or ""
    main = (yz or {}).get("main") or 0
    vwap = q.get("vwap")
    nzt, hi = (mood.get("by_board") or {}).get(s.get("board") or "", (0, 0))
    phase = (mood or {}).get("phase") or "不明"
    cap = board_limit_pct(s["code"])
    room = cap - chg
    bits = []
    score = 52

    if yday_dt_shape(q, yld) == "trap":
        return {
            "in_pool": True, "score": 8, "setup": "骗炮", "call": "不买",
            "why": "昨跌停骗炮，打板不做", "bits": ["骗炮"], "n7": n7, "n_lian": n_lian,
        }
    if limit_open_dump(s, q):
        return {
            "in_pool": True, "score": 10, "setup": "开后砸盘", "call": "不买",
            "why": "竞价涨停开后砸盘，出货不做", "bits": ["开后砸"], "n7": n7, "n_lian": n_lian,
        }

    # 昨板质量：一字板不打（买不到也接不住），烂板降级，硬板才配一进二
    yz_hard = yq.get("hard")
    yz_yizi = yq.get("yizi")
    yz_zbc = yq.get("zbc") or 0
    if yq.get("tag") != "无记录":
        bits.append("昨" + yq["tag"])
    rank_txt = (f or {}).get("board_rank") or ""
    rm = re.search(r"板内(\d+)/(\d+)", str(rank_txt))
    pos, n_in = (int(rm.group(1)), int(rm.group(2))) if rm else (99, 0)
    is_leader = pos <= 1 or (pos <= 2 and n_in >= 4)
    ma5, ma10 = (f or {}).get("ma5"), (f or {}).get("ma10")
    near_ma = any(m and abs(p / m - 1) <= 0.03 for m in (ma5, ma10))
    vr_n = vr_norm(vr)
    shrink = vr_n < 0.9

    setup = "打板观察"
    if today_zt:
        setup = "今板不追"
    elif yz_yizi:
        setup = "昨一字不打"
        score -= 12
        bits.append("昨一字/秒板，排不到也接不住")
    elif yday and n_lian == 1 and 3.0 <= gap <= 7.0 and yz_hard is not False and p >= o * 0.995:
        setup = "一进二"
        score += 14
        bits.append("昨首板(非烂板)今高开3-7%且站住开盘")
    elif yday and n_lian == 1 and 2.5 <= gap <= 7.0 and p < o * 0.995:
        # 高开后跌破开盘就不是一进二，是高开低走
        setup = "昨首板高开低走"
        score -= 10
        bits.append("高开后破开盘价，接力失败")
    elif yday and n_lian == 1 and 2.5 <= gap < 3.0:
        setup = "一进二(开口偏小)"
        score += 6
        bits.append("昨首板今高开不足3%")
    elif yday and n_lian == 1 and -3.2 <= gap <= -0.3 and chg > 0.4 and p > o and (yz_zbc >= 1 or yz_hard is False):
        setup = "弱转强"
        score += 14
        bits.append("昨烂板/炸板今低开翻红站开盘")
    elif yday and n_lian == 1 and -3.2 <= gap <= -0.3 and chg > 0.4 and p > o:
        setup = "低开翻红(昨板不弱)"
        score += 5
        bits.append("昨板不算弱，低开翻红只算普通接力")
    elif yday and n_lian >= 2 and chg < 7:
        setup = "二进三" if n_lian == 2 else "高位板回抽"
        score += 6 if n_lian == 2 else -6
        bits.append(f"昨{n_lian}连板回抽")
    elif n7 >= 2 and not yday and is_leader and near_ma and shrink and 0 <= chg < 5:
        setup = "龙回头"
        score += 10
        bits.append(f"板内{pos}位、缩量回踩均线")
    elif n7 >= 2 and not yday:
        setup = "断板回抽(非龙头/未回均线)"
        score -= 4
        bits.append(f"近7日{n7}板但{'非板内龙头' if not is_leader else '未缩量回均线'}")
    elif yday:
        setup = "昨板接力"
        score += 5
        bits.append("昨涨停待确认")

    # 今天自己封住了的话，看封单硬不硬（只影响描述和分，今板依旧不追）
    if tq and tq.get("tag") != "无记录":
        bits.append("今" + tq["tag"])

    # 空间压制：接近市场最高板的位置，胜率结构性变差
    mk_high = (mood or {}).get("high") or 0
    if mk_high and n_lian + 1 >= mk_high and n_lian >= 2:
        score -= 10
        bits.append(f"要打的是市场最高板附近({n_lian+1}/{mk_high})")

    # 赚钱效应：昨板今天整体亏钱，打板直接降级
    env = (mood or {}).get("env") or {}
    if env.get("prem") is not None:
        if env["prem"] <= -1.5:
            score -= 18
            bits.append(f"昨板今均{env['prem']:+.1f}%，亏钱效应")
        elif env["prem"] >= 1.5 and (env.get("adv") or 0) >= 15:
            score += 8
            bits.append(f"昨板今均{env['prem']:+.1f}%/晋级{env['adv']:.0f}%")

    if nzt >= 3:
        score += 12
        bits.append(f"题材{nzt}家助攻")
    elif nzt >= 1:
        score += 6
        bits.append(f"题材{nzt}家")
    else:
        score -= 8
        bits.append("孤板/题材0板")

    if st == "可做":
        score += 10
        bits.append("主线可做")
    elif st == "回避":
        score -= 18
        bits.append("主线回避")
    else:
        bits.append("主线中性")

    if yi is not None:
        if 20 <= yi <= 80:
            score += 10
            bits.append(f"市值{yi:.0f}亿适中")
        elif 12 <= yi <= 180:
            score += 5
            bits.append(f"市值{yi:.0f}亿可用")
        elif yi < 12 or yi > 400:
            score -= 8
            bits.append(f"市值{yi:.0f}亿不适合打板")

    if hs is not None:
        if 8 <= hs <= 22:
            score += 10
            bits.append(f"换手{hs:.1f}%充分")
        elif 5 <= hs <= 30:
            score += 5
            bits.append(f"换手{hs:.1f}%")
        elif hs < 3:
            score -= 10
            bits.append("换手清淡/一字嫌疑")
        elif hs > 40:
            score -= 8
            bits.append("换手爆量，接力差")

    if 3.0 <= gap <= 7.0:
        score += 6
        bits.append(f"开{gap:+.1f}%舒适区")
    elif gap >= 8.5:
        score -= 12
        bits.append("高开过大易砸")
    elif gap <= -4:
        score -= 8
        bits.append("低开过深")

    if auc_vr is not None:
        if auc_vr >= 3:
            score += 8
            bits.append(f"竞价量比{auc_vr:.1f}")
        elif auc_vr >= 1.5:
            score += 4
        elif auc_vr < 0.8 and gap > 2:
            score -= 8
            bits.append("高开缩量虚")
    if auc_call == "骗炮警惕":
        score -= 14
        bits.append("竞价骗炮")
    elif auc_call == "砸盘弱":
        score -= 8
        bits.append("竞价砸盘")
    elif auc_call == "抢筹强" and not today_zt:
        score += 4

    if n_lian == 1:
        score += 6
    elif n_lian == 2:
        score += 2
    elif n_lian >= 4:
        score -= 16
        bits.append("高位加速，胜率掉")
    if n7 >= 4:
        score -= 10
        bits.append("7日内过热")

    if main > 0:
        score += 6
        bits.append("主力净进")
    elif main < 0 and chg >= 2:
        score -= 10
        bits.append("价涨资金出")

    if phase == "发酵":
        score += 8
    elif phase == "高潮":
        score += 4
    elif phase == "修复":
        score += 2
    elif phase == "退潮":
        score -= 20
        bits.append("情绪退潮")

    if room < cap * 0.1:
        score -= 18
    elif room >= cap * 0.3:
        score += 3

    if vwap:
        if p >= vwap:
            score += 4
        else:
            score -= 6
            bits.append("均价下")
    if late:
        score -= 8
        bits.append("尾盘不新开")

    score = int(clip(score, 0, 100))

    call, why = "观察", "打板分不够或形态未确认，只盯不打"
    oh_d = overheat(s["code"], chg, q.get("name"))
    if today_zt or oh_d in ("见顶", "不追"):
        call, why = "不追", "今涨停/过热不追，等回抽或次日竞价"
    elif phase == "退潮":
        call, why = "观察", "情绪退潮，打板空仓"
    elif env.get("ok") is False:
        call, why = "观察", f"赚钱效应差（{env.get('txt') or '昨板今日弱'}），打板不新开"
    elif st == "回避":
        call, why = "观察", "主线回避，不打支线杂毛"
    elif late:
        call, why = "观察", "14:30后/收盘后打板不新开"
    elif yz_yizi:
        call, why = "观察", "昨一字板，次日不打"
    elif setup in ("一进二", "弱转强") and score >= 75 and room >= cap * 0.15:
        call, why = "可小仓", f"{setup}达标，小仓排队/回封，不追尖"
    elif setup == "龙回头" and score >= 80:
        call, why = "可小仓", "板内龙头缩量回踩均线转强，轻仓试，破今日低走"
    else:
        why = "；".join(bits[:4]) or why
    if call == "可小仓":
        call, why = hysteresis(s["code"] + ":db", call, why, score, 75, 70)

    return {
        "in_pool": True, "score": score, "setup": setup, "call": call,
        "why": why, "bits": bits, "n7": n7, "n_lian": n_lian,
        "yq": yq, "tq": tq, "leader_pos": pos,
        "factor": "；".join(bits[:6]) or "-",
    }


def structure_levels(hist, q, f):
    """昨高昨低、10/20日高低。均线来自 score_row。"""
    done = strip_today(hist) if hist else (hist or [])
    out = {
        "yday_h": None, "yday_l": None, "hi10": None, "hi20": None,
        "lo10": None, "lo20": None,
        "ma5": (f or {}).get("ma5"), "ma10": (f or {}).get("ma10"),
        "ma20": (f or {}).get("ma20"), "ma60": (f or {}).get("ma60"),
    }
    if done:
        out["yday_h"], out["yday_l"] = done[-1][2], done[-1][3]

        def mx(n):
            bars = done[-n:]
            h = max(b[2] for b in bars)
            if q and q.get("high"):
                h = max(h, q["high"])
            return h

        def mn(n):
            return min(b[3] for b in done[-n:])

        out["hi10"] = mx(min(10, len(done)))
        out["hi20"] = mx(min(20, len(done)))
        out["lo10"] = mn(min(10, len(done)))
        out["lo20"] = mn(min(20, len(done)))
    return out


def trade_exits(s, q, f, kind="游资", setup="", hist=None):
    """止盈止损估算，不是下单。
    业内常用几套叠在一起，再取「有效」的那档（离开现价够远，才算位）：
    1) 结构：今日低、昨低、10日低
    2) 均线：游资看 MA5/MA10，趋势看 MA20（均线是成本带，贴身均线当止损会变成碎止损）
    3) ATR / 吊灯：从近高往下扣 2～2.5 倍 ATR（机构趋势常用）
    4) 固定风险：主板大约 4.5%～6%，给波动留呼吸
    止盈：最近的前高（昨高/10日高/20日高）且至少约 1.5～2 倍风险；再上一档或涨停。
    距涨停不足 2.5 个点不设不到 1% 的碎止盈。
    """
    if not q:
        return None
    etf = kind == "ETF" or s.get("asset") == "etf" or "ETF" in (s.get("name") or "")
    daban = kind == "打板" or (setup or "").startswith(("一进二", "弱转强", "龙回头", "二进三", "连板回抽", "昨板接力"))
    if etf:
        style = "ETF"
    elif kind == "左侧" or setup == "左侧":
        style = "左侧"
    elif kind == "游资" or daban:
        style = "游资"
    else:
        style = "趋势"
    nd = 3 if etf else 2
    px = q["px"]
    prev = q.get("prev") or 0
    low = q.get("low") or px
    atrp = (f or {}).get("atr_pct") or (2.0 if etf else 3.0)
    atr = px * (atrp / 100.0)
    lim = limit_price(prev, s["code"], q.get("name")) if prev else None
    key = (f or {}).get("vwap_key_low")
    cm = is_20cm(s["code"])
    room = ((lim / px) - 1) if lim and px else None
    lv = structure_levels(hist, q, f)
    chandelier = None
    if lv.get("hi10") and atr:
        k = 2.2 if style == "趋势" else 2.0
        chandelier = lv["hi10"] - k * atr

    def pick_sl(cands, min_pct, hard_pct, max_pct, default_lab):
        usable = [(p, lab) for p, lab in cands if p is not None and p < px * (1 - min_pct + 1e-12)]
        if usable:
            sl, lab = max(usable, key=lambda x: x[0])
        else:
            sl, lab = px * (1 - hard_pct), default_lab
        if sl > px * (1 - min_pct):
            sl, lab = px * (1 - min_pct), lab + "/至少留波动"
        if sl < px * (1 - max_pct):
            sl, lab = px * (1 - max_pct), lab + "/封顶"
        return sl, lab

    cands = []
    if style == "左侧":
        # 左侧的失败定义本来就是「破今日低」，不能套游资/打板的均线止损，
        # 否则报告上写的止损和左侧战法自己的出场条件不是一回事。
        min_pct, hard_pct, max_pct = 0.025, 0.05, 0.07
        if low and low < px:
            cands.append((low * 0.997, "破今日低（左侧失败）"))
        if lv.get("lo10"):
            cands.append((lv["lo10"] * 0.997, "破10日低"))
        cands.append((px - 1.5 * atr, "1.5×ATR"))
        cands.append((px * (1 - hard_pct), f"左侧亏{hard_pct * 100:.0f}%"))
        sl, lab = pick_sl(cands, min_pct, hard_pct, max_pct, f"左侧亏{hard_pct * 100:.0f}%")
        tp1_floor, tp2_pct, r_mult = 0.05, 0.10, 2.0
        tp_note = "左侧反弹先看MA10/MA20或昨高，是反弹不是反转，到位先减"
    elif style == "游资":
        min_pct = (0.045 if cm else 0.03) if daban else 0.028
        # 20cm 打板日内波动 15%~25%，5.5% 的硬止损在噪音里，必须放宽并靠仓位控风险
        hard_pct = (0.085 if cm else 0.055) if daban else (0.07 if cm else 0.045)
        max_pct = (0.12 if cm else 0.08) if daban else (0.08 if cm else 0.055)
        if low and low < px:
            cands.append((low * 0.997, "破今日低"))
        if lv.get("yday_l"):
            cands.append((lv["yday_l"] * 0.997, "破昨低"))
        if lv.get("ma5"):
            cands.append((lv["ma5"] * 0.995, "破MA5"))
        if daban and lv.get("ma10"):
            cands.append((lv["ma10"] * 0.995, "破MA10"))
        if chandelier:
            cands.append((chandelier, "近高-2×ATR"))
        cands.append((px - 2.0 * atr, "2×ATR"))
        cands.append((px * (1 - hard_pct), f"短线亏{hard_pct * 100:.0f}%"))
        sl, lab = pick_sl(cands, min_pct, hard_pct, max_pct, f"短线亏{hard_pct * 100:.0f}%")
        tp1_floor, tp2_pct, r_mult = ((0.09, 0.18, 2.0) if cm else (0.055, 0.10, 2.0)) if daban else (
            (0.06, 0.12, 2.0) if cm else (0.05, 0.08, 2.0)
        )
        tp_note = "第一目标看昨高/10日高或2倍风险，余仓看到20日高或涨停"
    elif style == "ETF":
        min_pct, hard_pct, max_pct = 0.018, 0.03, 0.04
        if low and low < px:
            cands.append((low * 0.995, "今日低"))
        if lv.get("ma20"):
            cands.append((lv["ma20"] * 0.997, "破MA20"))
        cands.append((px - 1.5 * atr, "1.5×ATR"))
        cands.append((px * (1 - hard_pct), "亏3%"))
        sl, lab = pick_sl(cands, min_pct, hard_pct, max_pct, "亏3%")
        tp1_floor, tp2_pct, r_mult = 0.03, 0.06, 2.0
        tp_note = "ETF第一目标约3%或前高，余看6%"
    else:
        min_pct = 0.035
        hard_pct = 0.08 if cm else 0.06
        max_pct = 0.10 if cm else 0.08
        if key and key < px:
            cands.append((float(key), "分时关键低"))
        if lv.get("lo10"):
            cands.append((lv["lo10"] * 0.997, "破10日低"))
        if lv.get("ma20"):
            cands.append((lv["ma20"] * 0.995, "破MA20"))
        if lv.get("ma10"):
            cands.append((lv["ma10"] * 0.995, "破MA10"))
        if chandelier:
            cands.append((chandelier, "近高-2.2×ATR吊灯"))
        cands.append((px - 1.8 * atr, "1.8×ATR"))
        cands.append((px * (1 - hard_pct), f"趋势亏{hard_pct * 100:.0f}%"))
        sl, lab = pick_sl(cands, min_pct, hard_pct, max_pct, f"趋势亏{hard_pct * 100:.0f}%")
        tp1_floor, tp2_pct, r_mult = (0.07, 0.14, 2.0) if cm else (0.06, 0.12, 2.0)
        tp_note = "趋势第一目标看10日高或2倍风险，余仓看到20日高/涨停"

    risk = max(px - sl, px * min_pct)
    sl = min(sl, px - risk)
    r2 = px + r_mult * risk
    floor1 = px * (1 + tp1_floor)
    resists = []
    for p, name in (
        (lv.get("yday_h"), "昨高"),
        (lv.get("hi10"), "10日高"),
        (lv.get("hi20"), "20日高"),
        (lim, "涨停"),
    ):
        if p and p > px * 1.012:
            resists.append((p, name))
    # 去重：价差过近只留更远的那档标签
    resists.sort(key=lambda x: x[0])
    uniq = []
    for p, name in resists:
        if not uniq or p > uniq[-1][0] * 1.008:
            uniq.append((p, name))
    resists = uniq

    tp1, tp1_lab = max(r2, floor1), "2倍风险"
    valid_res = [(p, name) for p, name in resists if p >= px + 1.5 * risk and p >= floor1 * 0.98]
    if valid_res:
        tp1, tp1_lab = valid_res[0]
        if tp1 < floor1 and room and room >= tp1_floor:
            tp1 = floor1
            tp1_lab = tp1_lab + "/比例下限"
    tp2, tp2_lab = px * (1 + tp2_pct), "比例目标"
    later = [(p, name) for p, name in resists if p > tp1 * 1.01]
    if later:
        tp2, tp2_lab = later[0]
    elif lim and lim > tp1:
        tp2, tp2_lab = lim, "涨停"
    elif lv.get("hi20") and lv["hi20"] > tp1:
        tp2, tp2_lab = lv["hi20"], "20日高"

    at_high = bool(lv.get("hi20") and px >= lv["hi20"] * 0.995)
    if at_high:
        tp_note = "现价已在20日高附近，第一目标按2R/比例，第二目标看到涨停，不因回抽1个点就走"

    tight = room is not None and room <= 0.025
    if tight:
        if lim:
            tp1, tp2 = lim, lim
            tp1_lab = tp2_lab = "涨停"
        tp_note = "距涨停不足2.5个点，看到板或次日溢价，不设不到1%的碎止盈"
    elif lim:
        tp2 = min(max(tp2, tp1), lim)
        cap1 = px + (lim - px) * 0.65
        tp1 = min(tp1, cap1)
        if room >= 0.04 and tp1 < px * 1.04:
            tp1 = px * 1.04
        if tp1 >= lim:
            tp1 = min(lim * 0.992, px + (lim - px) * 0.6)
        if tp2 <= tp1:
            tp2, tp2_lab = lim, "涨停"

    if not tight and tp2 <= tp1:
        tp2 = lim if lim and lim > tp1 else px * (1 + max(tp2_pct, tp1_floor + 0.03))
        tp2_lab = "涨停" if lim and tp2 == lim else tp2_lab

    sl, tp1, tp2 = round(sl, nd), round(tp1, nd), round(tp2, nd)
    risk = max(px - sl, 1e-9)
    sl_pct = (sl / px - 1) * 100
    tp1_pct = (tp1 / px - 1) * 100
    tp2_pct = (tp2 / px - 1) * 100
    rr = (tp1 - px) / risk
    fmt = f"{{:.{nd}f}}"
    tag = "打板" if daban and style == "游资" else style
    sl_s = f"{fmt.format(sl)}({sl_pct:+.1f}%)"
    tp_s = f"{fmt.format(tp1)}(+{tp1_pct:.1f}%) / {fmt.format(tp2)}(+{tp2_pct:.1f}%)"
    refs = []
    if lv.get("ma5"):
        refs.append(f"MA5 {lv['ma5']:.{nd}f}")
    if lv.get("ma10"):
        refs.append(f"MA10 {lv['ma10']:.{nd}f}")
    if lv.get("ma20"):
        refs.append(f"MA20 {lv['ma20']:.{nd}f}")
    if lv.get("yday_h"):
        refs.append(f"昨高{lv['yday_h']:.{nd}f}")
    if lv.get("hi10"):
        refs.append(f"10日高{lv['hi10']:.{nd}f}")
    if lv.get("hi20"):
        refs.append(f"20日高{lv['hi20']:.{nd}f}")
    how = lab
    tp_txt = f"{tag}止盈{tp_s}（{tp1_lab} / {tp2_lab}；{tp_note}）"
    return {
        "sl": sl, "tp1": tp1, "tp2": tp2, "rr": rr, "how": how, "style": tag, "nd": nd,
        "sl_pct": sl_pct, "tp1_pct": tp1_pct, "tp2_pct": tp2_pct,
        "sl_lab": lab, "tp1_lab": tp1_lab, "tp2_lab": tp2_lab,
        "sl_txt": f"{tag}止损{sl_s}（{lab}）",
        "tp_txt": tp_txt,
        "sl_short": sl_s,
        "tp_short": tp_s,
        "refs": "，".join(refs),
        "txt": f"{tag} 止损{sl_s}（{lab}） 止盈{tp_s}（{tp1_lab}/{tp2_lab}） 盈亏比{rr:.1f} · {tp_note}",
    }

def verdict_etf(q, yz):
    how = (yz or {}).get("how") or ""
    sc = (yz or {}).get("score") or 0
    if "可小仓" in how and sc >= 60 and q["chg"] < 5:
        return "可小仓", how
    if sc >= 50:
        return "观察", how or "ETF观察"
    return "不买", how or "ETF不做"


def timing_pred(now, shapes, doable, avoid):
    t = now.hour * 60 + now.minute
    weak = any(x in ("高开低走", "低开冲高回落", "开后走弱") for x in shapes)
    bits = []
    if t < 11 * 60 + 30:
        bits.append("时点：上午。近一个月科技/医药轮动里，上午医药相对强，科技容易冲高回落。")
        if weak:
            bits.append("指数已冲高回落，上午后半段不接飞刀，看下午主线资金有没有接力。")
    elif t < 13 * 60:
        bits.append("时点：午休。上午结构已定，下午开盘看资金主线是否延续。")
    elif t < 14 * 60:
        bits.append("时点：13:00-14:00。科技若有日内反抽，多半先在这一段试；医药短线这轮往往上午强、下午开始钝化。")
    elif t < 14 * 60 + 30:
        bits.append("时点：14:00-14:30，是科技日内反抽的常见窗口；没有资金回流就只是弱修复。")
    else:
        bits.append("时点：14:30后到尾盘。不追新高，只看主线资金有没有把早盘流出收住。")
    if weak:
        bits.append("大盘形态是冲高回落后的修复，时点上偏向做资金还在进的线，不因为自选科技分高就改做科技。")
    if doable:
        bits.append("资金当前认可：" + "、".join(doable) + "。只在这条线上对照筛选票。")
    if avoid:
        bits.append("资金当前回避：" + "、".join(avoid) + "。")
    return bits


TECH_LINES = {"光通信", "PCB", "半导体", "算力硬件", "算力液冷", "电子元件", "消费电子"}
MED_LINES = {"创新药", "游资医药", "医疗", "中药", "医药"}


def auction_pts(auc_call):
    """值分竞价加减。只轻推排序，不改买点闸。抢筹最多+3，骗炮最多-3。"""
    return {
        "抢筹强": 3, "承接关注": 1, "正常": 0,
        "砸盘弱": -2, "骗炮警惕": -3, "竞价缺": 0,
    }.get(auc_call or "", 0)


def role_of(call, st):
    if call == "可小仓":
        return "可买"
    if call == "可试仓":
        return "轻仓备选"
    if call == "不追":
        return "不追"
    if call == "不买":
        return "不买"
    if st == "可做":
        return "备选(主线热)"
    if st == "回避":
        return "备选(板块冷)"
    return "备选"


def hand_of(call):
    if call == "可小仓":
        return "可以下手（小仓）"
    if call == "可试仓":
        return "轻仓试，不替代右侧"
    if call == "观察":
        return "先盯，不能下手"
    if call == "不追":
        return "不能下手（不追）"
    return "不能下手"


def heat_pts_of(st, line, heat_map):
    """主线热度只认本线主力，不借光通信/电子的钱给液冷、光纤、PCB。
    别名 _heat_pts_of 给主流程用，保证批量报告和单股分析页同一套口径。"""
    h = {}
    if line in (heat_map or {}):
        h = heat_map[line]
    elif line in MED_LINES:
        for k in ("医药", "医疗", "医疗研发外包"):
            if k in (heat_map or {}):
                h = heat_map[k]
                break
    amt = h.get("amt") or 0
    if st == "可做":
        pts = 22 + clip(amt / 4.0, 0, 12)
    elif st == "中性":
        pts = 8 + clip(amt / 6.0, 0, 6)
    else:
        pts = clip(amt / 8.0, -8, 0)
    tag = f"{line}/{st}"
    if amt:
        tag += f" 主力{amt:+.0f}亿"
    return pts, tag


def line_status_of(s, doable, avoid):
    """批量报告和单股分析共用这一份。ETF 走 ETF_LINE 重映射。"""
    line = line_of_board(s.get("board"))
    if s.get("asset") == "etf" or "ETF" in (s.get("name") or ""):
        line = ETF_LINE.get(s.get("name") or "", line or "ETF")
    if line in avoid:
        return "回避", line
    if line == "中药" and "医药" in doable:
        return "中性", line
    if line in doable:
        return "可做", line
    if line in MED_LINES and line != "中药" and "医药" in doable:
        return "可做", line
    return "中性", line


# 盘面分内部已经含「板块资金」和「竞价质量」的战法。值分里不能再整份加一遍
TAPE_HAS_SECTOR_AUC = {"游资", "打板", "ETF"}


def worth_pts(call, kind, st, line, tape, auc_call=None, ma_score=None, heat_map=None):
    """值分 = 闸 + 主线热 + 盘面×0.28 + 均线分×0.18 + 竞价。
    去重：游资/打板/ETF 的盘面分（7a/打板分）里已经含板块资金和竞价质量，
    这里主线热度只按 0.45 计、竞价不再重复加；趋势用的是买点分，不含这两项，全额计。
    不这么做，同一条信息会在值分里算两到三次，游资/打板被系统性抬高，排序失真。"""
    hp, htag = heat_pts_of(st, line, heat_map or {})
    cp = {"可小仓": 50, "可试仓": 24, "观察": 18}.get(call, 0)
    kp = 0
    if call == "可小仓":
        kp = {"趋势": 8, "游资": 5, "打板": 7, "ETF": 2}.get(kind, 0)
    elif call == "可试仓":
        kp = 1
    dedup = kind in TAPE_HAS_SECTOR_AUC
    hp_eff = hp * 0.45 if dedup else hp
    ap = 0 if dedup else auction_pts(auc_call)
    ma = clip(ma_score or 0, 0, 100)
    return cp + kp + hp_eff + 0.28 * (tape or 0) + 0.18 * ma + ap, htag, ap


_line_status_of = line_status_of
_heat_pts_of = heat_pts_of
_worth_pts = worth_pts


def infer_market(code):
    if code.startswith(("6", "5", "9", "688")):
        return "sh"
    return "sz"


def _n(x, d=2, suf=""):
    if x is None:
        return "—"
    try:
        return f"{float(x):.{d}f}{suf}"
    except (TypeError, ValueError):
        return "—"


def _yi(x):
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "—"
    if abs(v) < 1e4:
        return f"{v:+.0f}元"
    return f"{v / 1e8:+.2f}亿"


def explain_analyze(s, q, fac, yz, st, line, kind, call, why, auc, db, left, ex,
                    mood, late, doable, avoid, flow, htag, sc, tape_txt, is_etf):
    """单次分析的可读依据。闸结论仍用 call/why，这里只把数据和原因摊开。"""
    fac = fac or {}
    yz = yz or {}
    q = q or {}
    auc = auc or {}
    reasons = []

    def add(k, v):
        v = (v or "").strip()
        if v:
            reasons.append({"k": k, "v": v})

    px, o, h, lo = q.get("px"), q.get("open"), q.get("high"), q.get("low")
    prev, vwap = q.get("prev"), q.get("vwap")
    chg = q.get("chg")
    vr = q.get("vol_ratio")
    hs = q.get("turnover")
    ma5, ma10, ma20 = fac.get("ma5"), fac.get("ma10"), fac.get("ma20")
    rsi, dd = fac.get("rsi"), fac.get("dd")
    vs_vwap = ((px / vwap - 1) * 100) if px and vwap else None
    vs_ma20 = ((px / ma20 - 1) * 100) if px and ma20 else None
    gap = ((o / prev - 1) * 100) if o and prev else None

    add("结论", f"{hand_of(call)}。买点闸是「{call}」：{why}。值分 { _n(sc, 0) } 只排队，不替代闸。")

    loc = []
    loc.append(f"现价{_n(px)}，今{_n(chg, 2, '%')}，开{_n(o)} 高{_n(h)} 低{_n(lo)}，昨收{_n(prev)}")
    if gap is not None:
        loc.append(f"开幅{gap:+.2f}%")
    if vwap:
        loc.append(f"分时均价{_n(vwap)}" + (f"（现价相对{vs_vwap:+.2f}%）" if vs_vwap is not None else ""))
    ma_bits = []
    if ma5:
        ma_bits.append(f"MA5 {_n(ma5)}")
    if ma10:
        ma_bits.append(f"MA10 {_n(ma10)}")
    if ma20:
        ma_bits.append(f"MA20 {_n(ma20)}" + (f" 距{vs_ma20:+.1f}%" if vs_ma20 is not None else ""))
    if rsi is not None:
        ma_bits.append(f"RSI {_n(rsi, 1)}")
    if dd is not None:
        ma_bits.append(f"回撤{dd * 100:.1f}%")
    if ma_bits:
        loc.append("；".join(ma_bits))
    add("价格位置", "。".join(loc) + "。")

    gate = []
    if is_etf:
        gate.append(f"走ETF闸，盘面分{tape_txt}")
    elif kind == "游资":
        gate.append(f"走游资闸，7a { _n(yz.get('score'), 0) }（满仓线65），{yz.get('how') or ''}")
        if late:
            gate.append("14:30后游资默认不新开")
        if (mood or {}).get("phase") == "退潮":
            gate.append("全市场情绪退潮，游资空仓优先")
        hs_ok = (hs is not None and hs >= 5) or (vr or 0) >= 1.5
        gate.append(
            f"换手{_n(hs, 2, '%')}、量比{_n(vr, 2)}"
            + ("，量能够门槛" if hs_ok else "，换手要≥5%或量比≥1.5才过闸")
        )
    elif kind == "打板":
        gate.append(f"打板仓过闸：{(db or {}).get('setup') or ''} {why}")
        gate.append("打板可小仓不占用7a≥65，但今涨停不追、昨跌停骗炮、主线回避、退潮仍一票否决")
    else:
        cap = trend_chg_cap(s["code"], fac.get("atr_pct"))
        gate.append(f"走趋势闸。今涨上限约{cap:.1f}%（ATR/板），站上均价且未破关键低才谈小仓")
        if fac.get("vwap_reclaim"):
            gate.append("属于先破均价再收回" + ("，且守住当日关键低" if fac.get("vwap_held") else "，但已破当日关键低"))
        if (vr or 0) < 1:
            gate.append(f"量比{_n(vr, 2)}偏弱，无量站上均价胜率差")
        if fac.get("vp") == "价涨资金出":
            gate.append("价涨资金出，趋势不能小仓")
        if rsi is not None and rsi >= 70:
            gate.append(f"RSI {rsi:.1f}≥70，过热等回踩")
    if st == "回避" and call in ("观察", "不买"):
        gate.append(f"所属主线「{line}」资金净出，回避的是这条线自己的钱在出，不是因为涨得多、也不是兄弟板块连坐")
    add("买点闸", "。".join(x for x in gate if x) + "。")

    inn_s = "、".join(list(doable)[:8]) or "暂无净流入主线"
    out_s = "、".join(list(avoid)[:8]) or "暂无净流出主线"
    add(
        "主线资金",
        f"这只归属板块「{s.get('board') or '-'}」→ 主线「{line}」，状态「{st}」（{htag}）。"
        f"今日可做：{inn_s}。今日回避：{out_s}。热门涨幅不等于主力在进。",
    )

    main = (flow or {}).get("main")
    main5 = (flow or {}).get("main5")
    xlarge = (flow or {}).get("xlarge")
    add(
        "个股资金",
        f"今主力{_yi(main)}，超大单{_yi(xlarge)}，近5日主力{_yi(main5)}。"
        f"{yz.get('same_txt') or ''}。量价标注：{fac.get('vp') or '-'}。{yz.get('flow_txt') or ''}",
    )

    add(
        "量能盘面",
        f"量比{_n(vr, 2)}，换手{_n(hs, 2, '%')}，振幅{_n(q.get('amp'), 2, '%')}。"
        f"{fac.get('orb') or ''}；{fac.get('pullback') or ''}；{fac.get('slope_txt') or ''}；"
        f"{fac.get('yhl') or ''}；{fac.get('vwap_pos') or ''}；{fac.get('pos') or ''}；"
        f"{fac.get('rs_txt') or ''}。买点分{_n(fac.get('entry'), 0)}，均线分{_n(fac.get('buy'), 0)}。",
    )

    abits = "，".join(auc.get("bits") or [])
    add(
        "集合竞价",
        f"{auc.get('call') or '竞价缺'}：{auc.get('why') or '无'}。"
        f"{abits + '。' if abits else ''}"
        f"开后现价相对开盘{'站稳' if px and o and px >= o else '已弱于开盘'}。",
    )

    if not is_etf and yz:
        marks = yz.get("marks") or {}
        mk = "；".join(f"{a}{b}" for a, b in marks.items()) if marks else (yz.get("factor_line") or "")
        add(
            "游资7a",
            f"{_n(yz.get('score'), 0)}分，{yz.get('how') or ''}。"
            f"板块：{yz.get('sec_txt') or '-'}。弹性{yz.get('elast_mark') or '-'}，"
            f"距涨停还剩约{_n(yz.get('room'), 1, '%')}。因子：{mk}。",
        )

    if db and db.get("in_pool"):
        bits = "；".join(db.get("bits") or [])
        add(
            "打板战法",
            f"{db.get('setup') or ''}，打板分{_n(db.get('score'), 0)}，闸「{db.get('call') or ''}」。"
            f"{db.get('why') or ''}。近7日涨停{db.get('n7') or 0}次，连板{db.get('n_lian') or 0}。"
            + (f"细节：{bits}。" if bits else ""),
        )
    elif not is_etf:
        add("打板战法", "近7日无涨停，不进打板池。游资仓仍看7a，趋势仓看均价/均线闸。")

    phase = (mood or {}).get("phase") or "不明"
    add(
        "情绪时点",
        f"市场情绪「{phase}」。"
        + ("已过14:30，游资/打板不新开。" if late else "盘中时段，仍可按闸排队。")
        + "左侧超跌不能替代右侧买点。",
    )

    if left and left.get("call"):
        add(
            "左侧超跌",
            f"{left.get('call')}：{left.get('how') or ''}。超跌分{_n(left.get('score'), 0)}。"
            "只作轻仓试错备注，过不了右侧闸就不能当主仓。",
        )

    if ex:
        add(
            "止盈止损",
            f"{ex.get('txt') or ''}。"
            + (f"参照：{ex.get('refs')}。" if ex.get("refs") else "")
            + "止损叠了结构位、均线、ATR吊灯和固定百分比，只取离开现价够远的那档。"
            "止盈先看昨高/10日高，再看20日高或涨停。这是过闸后的纪律价，不是预测。",
        )
    else:
        add("止盈止损", "买点闸没过，不算止盈止损价，避免假装有计划。")

    add(
        "怎么用",
        "先看买点闸能不能下手，再看主线是不是回避，再看竞价有没有骗炮。"
        "值分只在能买的里面排队。单次分析不写入自选，也不代下单。",
    )
    return reasons


def analyze_one(code, board="自选", kind_hint=""):
    """单票即时分析。闸/值分与全池快照同一套，不写死代码。"""
    code = re.sub(r"\D", "", str(code or ""))
    if len(code) != 6:
        return {"ok": False, "error": "代码必须是6位数字"}
    mods = []

    def ran(name, ok=True, note=""):
        mods.append({"name": name, "ok": bool(ok), "note": note or ""})

    first = infer_market(code)
    markets = [first, "sz" if first == "sh" else "sh"]
    extra = ["sh000001", "sz399006", "sh512480", "sh515050", "sh512010"]
    live, q, market = {}, None, first
    for m in markets:
        try:
            live = tencent([m + code] + extra)
        except Exception:
            live = {}
        q = live.get(code)
        if q and q.get("name") and q.get("px", 0) > 0:
            market = m
            break
    ran("实时行情", bool(q))
    if not q:
        return {"ok": False, "error": "行情未找到该代码（检查市场或是否停牌）", "modules": mods,
                "module_ok": sum(1 for x in mods if x["ok"]), "module_n": len(mods)}
    name = q["name"]
    is_etf = (
        kind_hint == "etf"
        or "ETF" in name.upper()
        or "基金" in name
        or code.startswith(("15", "51", "56", "58", "16"))
    )
    # 已在自选里的票，用自选登记的板块，否则主线判定和板块同向都会落到「自选」这个假板块上
    wl_board = ""
    for x in (WL.get("stocks") or []) + (WL.get("etfs") or []):
        if x.get("code") == code:
            wl_board = x.get("board") or ""
            break
    s = {
        "code": code, "market": market, "name": name,
        "board": "ETF" if is_etf else (wl_board or board or "自选"),
        "asset": "etf" if is_etf else "stock",
    }
    try:
        raw = daily_hist(s)
    except Exception:
        raw = []
    hist = strip_today(raw) or raw
    ran("日线", bool(hist), f"{len(hist)}根" if hist else "缺")
    try:
        minutes = tencent_minute(s)
    except Exception:
        minutes = []
    gate_state_load()
    ran("分时", bool(minutes), f"{len(minutes)}点" if minutes else "缺")
    fac = score_row(hist, q, s["code"]) if hist and q else None
    ran("均线分", bool(fac))
    if not fac:
        return {"ok": False, "error": "日线不足，暂时分析不了", "modules": mods,
                "module_ok": sum(1 for x in mods if x["ok"]), "module_n": len(mods)}
    fac["_min"] = minutes
    fac["_raw"] = raw
    yld = False if is_etf else yday_limit_down(code, hist)
    ran("昨跌停", True, "ETF跳过" if is_etf else ("昨跌停" if yld else "否"))
    try:
        inn, outf = sector_flow()
    except Exception:
        inn, outf = [], []
    ran("板块资金", bool(inn or outf), f"入{len(inn)}出{len(outf)}")
    inn_lines, out_lines = flow_sets(inn, outf)
    doable, avoid = line_policy(inn_lines, out_lines)
    heat_map = flow_heat_map(inn, outf)
    try:
        flows = stock_flow([s])
    except Exception:
        flows = {}
    flow = flows.get(code) or {}
    ran("个股资金", bool(flow))
    bench = {}
    for c, m in (("000001", "sh"), ("399006", "sz")):
        try:
            h = daily_hist({"code": c, "market": m})
            bench[c] = strip_today(h) or h
        except Exception:
            bench[c] = []
    idx_weak, _ = index_left_weak(live, bench)
    ran("大盘对照", any(bench.values()))
    bcode = BOARD_BENCH.get(s.get("board") or "")
    if not bcode:
        bcode = "399006" if is_20cm(code) else "000001"
    bench_r5 = ret5(bench.get(bcode) or bench.get("000001") or [], q["px"])
    fac.update(trend_annot(hist, q, fac, flow, bench_r5, minutes))
    ran("趋势盘面", True)
    fac["entry"] = entry_score(fac, q)
    ran("买点分", fac.get("entry") is not None)
    fac["auction"] = auction_judge(s, q, fac, hist, yld)
    ran("集合竞价", bool((fac.get("auction") or {}).get("call")))
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    zt_t_rows, zt_y_rows = [], []
    try:
        zt_t_rows = zt_pool()
        zt_y_rows = zt_pool(prev_trade_date_str(now, hist))
    except Exception:
        pass
    try:
        env = daban_env(zt_y_rows)
    except Exception:
        env = {}
    try:
        mood = market_mood(zt_t_rows, env)
    except Exception:
        mood = {"phase": "不明"}
    ran("情绪", bool(mood.get("phase") and mood.get("phase") != "不明"), mood.get("phase") or "")
    ran("涨停池", bool(zt_t_rows or zt_y_rows), f"今{len(zt_t_rows)}/昨{len(zt_y_rows)}")
    late = now.hour > 14 or (now.hour == 14 and now.minute >= 30)
    # 板块热度要和批量报告同一个口径：统计自选里同板块的票有几只在涨且主力净进。
    # 过去这里只放当前这一只（样本=1），7a 的「个股热钱同向」那一档永远不可能触发，
    # 同一只票在网页和报告上 7a 分数会差几分，刚好能跨过 65 那条线。
    b = s.get("board") or ""
    peers = [x for x in (WL.get("stocks") or []) if (x.get("board") or "") == b]
    if all(x.get("code") != code for x in peers):
        peers = peers + [{"code": code, "market": s.get("market") or infer_market(code), "board": b}]
    up, n_b = 0, 0
    pq = {}
    try:
        pq = tencent([("sh" if (x.get("market") or infer_market(x["code"])) == "sh" else "sz") + x["code"]
                      for x in peers]) if len(peers) > 1 else {}
    except Exception:
        pq = {}
    pflow = {}
    try:
        pflow = stock_flow(peers) if len(peers) > 1 else {}
    except Exception:
        pflow = {}
    for x in peers:
        pqq = pq.get(x["code"]) if x["code"] != code else q
        if not pqq:
            continue
        n_b += 1
        mf = (pflow.get(x["code"]) or (flow if x["code"] == code else {}) or {}).get("main") or 0
        if pqq["chg"] > 0 and mf > 0:
            up += 1
    if not n_b:
        up, n_b = (1 if q["chg"] > 0 and (flow.get("main") or 0) > 0 else 0), 1
    board_heat = {b: (up, n_b)}
    ran("板块同向", n_b > 0, f"{up}/{n_b}只同向")
    yz = youzi_score(s, q, fac, yld, flow, inn_lines, out_lines, board_heat, hist) if q else {}
    kind = "ETF" if is_etf else stock_kind(s, q, hist)
    st, line = line_status_of(s, doable, avoid)
    if is_etf:
        call, why = verdict_etf(q, yz)
        kind = "ETF"
        tape = (yz or {}).get("score") or 0
        tape_txt = f"ETF {tape:.0f}"
        gate_name = "ETF闸"
    elif kind == "游资":
        call, why = verdict_youzi(s, q, fac, yld, yz, st, mood, late)
        tape = (yz or {}).get("score") or 0
        tape_txt = f"7a {tape:.0f}"
        gate_name = "游资闸"
    else:
        call, why = verdict_trend(s, q, fac, yld, st)
        kind = "趋势"
        tape = (fac or {}).get("entry") or 0
        tape_txt = f"买点{tape:.0f}"
        gate_name = "趋势闸"
    auc = ((fac or {}).get("auction") or {})
    ma_score = (fac or {}).get("buy") or 0
    sc, htag, ap = worth_pts(call, kind, st, line, tape, auc.get("call"), ma_score, heat_map)
    left = None
    if fac and not is_etf:
        try:
            ls = left_setup(s, q, fac, yld, hist, flow, idx_weak)
            if ls:
                # 和批量报告同一套：板块在出钱时，左侧只盯不抄
                if ls["call"] == "可试仓" and st == "回避" and ls.get("kind") != "游资":
                    ls["call"] = "观察"
                    ls["how"] = "超跌够了，板块资金还在出，只盯不抄"
                left = {"call": ls.get("call"), "how": ls.get("how"), "score": ls.get("score")}
        except Exception:
            left = None
    ran("游资7a", bool(yz), "" if yz else "未计")
    ran(gate_name, True, call or "")
    ran("值分", True, f"{sc:.0f}")
    db = {}
    if not is_etf:
        db = daban_plan(s, q, fac, hist, yz, st, line, mood, late, yld,
                        zt_map(zt_y_rows), zt_map(zt_t_rows))
        if db.get("call") == "可小仓" and call != "可小仓" and call not in ("不买", "不追"):
            call, why, kind = db["call"], db["why"], "打板"
            tape = db.get("score") or 0
            tape_txt = f"打板{tape:.0f}"
            sc, htag, ap = worth_pts(call, kind, st, line, tape, auc.get("call"), ma_score, heat_map)
        elif db.get("call") == "可小仓" and call == "可小仓":
            kind = "打板"
            why = db["why"] + "；" + why
    ran("打板战法", bool(db.get("in_pool")), (db.get("setup") or "未入池") + f" {db.get('score', 0)}")
    ex = trade_exits(s, q, fac, kind, (db or {}).get("setup") or "", hist) if call in ("可小仓", "可试仓") else None
    ran("左侧超跌", True, (left or {}).get("call") if left else ("ETF跳过" if is_etf else "未形成"))
    ok_n = sum(1 for x in mods if x["ok"])
    reasons = explain_analyze(
        s, q, fac, yz, st, line, kind, call, why, auc, db, left, ex,
        mood, late, doable, avoid, flow, htag, sc, tape_txt, is_etf,
    )
    return {
        "ok": True,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "item": {
            "code": code, "market": market, "name": name, "board": s["board"],
            "px": q["px"], "chg": q["chg"], "open": q.get("open"), "high": q.get("high"),
            "low": q.get("low"), "vol_ratio": q.get("vol_ratio"), "vwap": q.get("vwap"),
            "turnover": q.get("turnover"), "kind": "etf" if is_etf else "stock",
        },
        "kind": kind,
        "call": call,
        "why": why,
        "reasons": reasons,
        "hand": hand_of(call),
        "role": role_of(call, st),
        "line": htag,
        "score": round(sc, 1),
        "scores": {
            "worth": round(sc, 1),
            "ma": round(ma_score, 1),
            "tape": round(tape or 0, 1),
            "tape_txt": tape_txt,
            "entry": round((fac or {}).get("entry") or 0, 1),
            "yz": round((yz or {}).get("score") or 0, 1),
            "auction": auc.get("call") or "竞价缺",
            "auction_pts": ap,
            "auction_why": auc.get("why") or "",
        },
        "tape": {
            "vp": (fac or {}).get("vp") or "-",
            "orb": (fac or {}).get("orb") or "-",
            "vwap_pos": (fac or {}).get("vwap_pos") or "-",
            "pullback": (fac or {}).get("pullback") or "-",
            "pos": (fac or {}).get("pos") or "-",
            "yhl": (fac or {}).get("yhl") or "-",
            "slope": (fac or {}).get("slope_txt") or "-",
        },
        "left": left,
        "mood": (mood or {}).get("phase") or "-",
        "daban": db or None,
        "exits": ex,
        "modules": mods,
        "module_ok": ok_n,
        "module_n": len(mods),
    }


def main():
    global WL
    WL = load_watchlist()
    gate_state_load()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    stocks, etfs, idx = WL["stocks"], WL["etfs"], WL["index"]
    extra = [
        "hkHSI", "sz159941", "sh513100", "sh512480", "sz159813", "sh515050",
        "sh512010", "sh000933", "sz399989",
    ]
    codes = extra[:]
    for x in idx + stocks + etfs:
        codes.append(("sh" if x["market"] == "sh" else "sz") + x["code"])
    live = tencent(codes)

    idx_lines, shapes = [], []
    for x in idx:
        q = live.get(x["code"])
        if q:
            shp = index_shape(q)
            shapes.append(shp)
            idx_lines.append(
                f"{x['name']} {q['px']:.2f} {q['chg']:+.2f}% 开{q['open']:.2f} 高{q['high']:.2f} 低{q['low']:.2f} {shp} 量比{q['vol_ratio']:.2f}"
            )

    ext_map = [
        ("HSI", "恒生"),
        ("159941", "纳指ETF"),
        ("513100", "纳指ETF博时"),
        ("512480", "半导体ETF"),
        ("159813", "半导体ETF鹏华"),
        ("515050", "通信ETF"),
        ("512010", "医药ETF"),
        ("000933", "中证医药"),
        ("399989", "中证医疗"),
    ]
    ext_lines = []
    for code, name in ext_map:
        q = live.get(code)
        if q:
            ext_lines.append(f"{name} {q['px']:.3f} {q['chg']:+.2f}% 高{q['high']:.3f} 低{q['low']:.3f}")

    etf_lines = []
    for e in etfs:
        q = live.get(e["code"])
        if q:
            etf_lines.append(f"{e['name']} {q['px']:.3f} {q['chg']:+.2f}%")

    inn, outf = sector_flow()
    etf_items = []
    for e in etfs:
        ee = dict(e)
        ee["asset"] = "etf"
        ee.setdefault("board", "ETF")
        etf_items.append(ee)
    flows = stock_flow(stocks + etf_items)
    try:
        ovn_scan = overnight_scan(stocks, etfs)
    except Exception:
        ovn_scan = {
            "summary": [], "indices": [], "sectors": [], "metals": [], "related": [],
            "themes": [], "leaders": [], "laggers": [], "bias": "数据暂缺",
            "bias_why": "隔夜报价拉取失败", "picks": [], "avoid": [], "oil_note": "",
            "avg_idx": 0,
        }
    if ovn_scan.get("indices") or ovn_scan.get("themes"):
        _save_ovn_cache(ovn_scan)
    elif not (ovn_scan.get("leaders") or ovn_scan.get("themes")):
        cached = _load_ovn_cache()
        if cached:
            ovn_scan = cached
            ovn_scan["_from_cache"] = True
        else:
            seeded = _seed_themes_from_macro()
            if seeded:
                ovn_scan["themes"] = seeded
                ovn_scan["leaders"] = seeded
                ovn_scan["bias"] = ovn_scan.get("bias") or "中性分化"
                ovn_scan["bias_why"] = "实时报价暂缺，主题来自宏观笔记，只跟领涨主题"
    ovn = ovn_scan.get("summary") or []

    def one_name(s, asset="stock"):
        item = dict(s)
        item["asset"] = asset
        if asset == "etf":
            item.setdefault("board", "ETF")
        q = live.get(item["code"])
        try:
            raw = daily_hist(item)
        except Exception:
            raw = []
        hist = strip_today(raw) or raw
        try:
            minutes = tencent_minute(item)
        except Exception:
            minutes = []
        fac = score_row(hist, q, item["code"]) if hist and q else None
        if fac:
            fac["_min"] = minutes
            fac["_raw"] = raw
        yld = yday_limit_down(item["code"], hist) if hist and asset != "etf" else False
        return item, q, fac, yld, hist

    rows = []
    etf_rows = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(one_name, s, "stock") for s in stocks]
        futs += [ex.submit(one_name, e, "etf") for e in etfs]
        for fut in as_completed(futs):
            item, q, fac, yld, hist = fut.result()
            if item.get("asset") == "etf":
                etf_rows.append((item, q, fac, yld, hist))
            else:
                rows.append((item, q, fac, yld, hist))
    rows.sort(key=lambda r: next((i for i, x in enumerate(stocks) if x["code"] == r[0]["code"]), 0))
    etf_rows.sort(key=lambda r: next((i for i, x in enumerate(etfs) if x["code"] == r[0]["code"]), 0))

    bench_specs = [
        ("000001", "sh"), ("399006", "sz"), ("512480", "sh"),
        ("515050", "sh"), ("512010", "sh"), ("512400", "sh"), ("159698", "sz"),
    ]
    bench_hist, idx_hist = {}, {}

    def one_bench(code, mkt):
        item = {"code": code, "market": mkt}
        try:
            h = daily_hist(item)
            return code, strip_today(h) or h
        except Exception:
            return code, []

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(one_bench, c, m) for c, m in bench_specs]
        for fut in futs:
            code, h = fut.result()
            bench_hist[code] = h
            if code in ("000001", "399006"):
                idx_hist[code] = h
    bench_r5s = {}
    for code, h in bench_hist.items():
        lq = live.get(code)
        bench_r5s[code] = ret5(h, lq["px"] if lq else None)
    zt_t_rows, zt_y_rows = [], []
    try:
        zt_t_rows = zt_pool()
        zt_y_rows = zt_pool(prev_trade_date_str(now, idx_hist.get("000001")))
    except Exception:
        pass
    zt_t, zt_y = zt_map(zt_t_rows), zt_map(zt_y_rows)
    try:
        db_env = daban_env(zt_y_rows, live)
    except Exception:
        db_env = {}
    try:
        mood = market_mood(zt_t_rows, db_env)
    except Exception:
        mood = {
            "phase": "不明", "n_zt": 0, "n_dt": 0, "n_20": 0,
            "note": "涨停统计暂缺", "by_board": {}, "txt": "情绪：暂缺",
            "ladder": {}, "high": 0, "n_lian": 0, "env": db_env or {},
        }
    try:
        lhb = lhb_warn_map()
    except Exception:
        lhb = {}
    idx_weak, idx_weak_why = index_left_weak(live, idx_hist)

    def fill_trend(s, q, f, hist):
        if not f or not q:
            return
        bcode = BOARD_BENCH.get(s.get("board") or "")
        if not bcode:
            bcode = "399006" if is_20cm(s["code"]) else "000001"
        f.update(trend_annot(hist, q, f, flows.get(s["code"]), bench_r5s.get(bcode), f.get("_min")))

    for s, q, f, yld, hist in rows:
        fill_trend(s, q, f, hist)
    for s, q, f, yld, hist in etf_rows:
        fill_trend(s, q, f, hist)

    board_r5s, board_chgs = {}, {}
    for s, q, f, yld, hist in rows:
        b = s.get("board") or ""
        if f and f.get("r5") is not None:
            board_r5s.setdefault(b, []).append(f["r5"])
        if q:
            board_chgs.setdefault(b, []).append((s["code"], q["chg"]))
    board_avg = {b: sum(vs) / len(vs) for b, vs in board_r5s.items() if vs}
    board_rank = {}
    for b, items in board_chgs.items():
        ordered = sorted(items, key=lambda x: -x[1])
        n = len(ordered)
        for i, (code, _) in enumerate(ordered, 1):
            board_rank[code] = f"板内{i}/{n}"
    for s, q, f, yld, hist in rows + etf_rows:
        if not f:
            continue
        avg = board_avg.get(s.get("board") or "")
        if f.get("r5") is not None and avg is not None:
            f["board_rs"] = f"板块{(f['r5'] - avg) * 100:+.1f}%"
        f["board_rank"] = board_rank.get(s["code"], "-")
        f["entry"] = entry_score(f, q)
        f["auction"] = auction_judge(s, q, f, hist, yld)

    ranked = []
    for s, q, f, yld, hist in rows:
        if not f or not q:
            continue
        if stock_kind(s, q, hist) != "趋势":
            continue
        if is_limit_up(s["code"], q["chg"]):
            continue
        ranked.append((s, q, f, yld))
    ranked.sort(key=lambda r: -r[2]["buy"])

    inn_lines, out_lines = flow_sets(inn, outf)
    board_heat = {}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        b = s.get("board") or ""
        up, n = board_heat.get(b, (0, 0))
        fl = flows.get(s["code"]) or {}
        n += 1
        if q["chg"] > 0 and (fl.get("main") or 0) > 0:
            up += 1
        board_heat[b] = (up, n)
    yz_ranked = []
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        yz = youzi_score(s, q, f, yld, flows.get(s["code"]), inn_lines, out_lines, board_heat, hist)
        yz["annot"] = youzi_annot(s, q, f, hist, flows.get(s["code"]), mood, lhb)
        yz_ranked.append((s, q, f, yld, yz))
    yz_ranked.sort(key=lambda r: -r[4]["score"])
    yz_by_code = {x[0]["code"]: x[4] for x in yz_ranked}
    yz_youzi = [x for x in yz_ranked if x[4]["kind"] == "游资"]
    try:
        ovn_scan = overnight_follow_desk(
            ovn_scan, stocks, etfs, rows, etf_rows, yz_by_code, inn, outf, inn_lines, out_lines,
        )
    except Exception:
        pass
    etf_yz = []
    for s, q, f, yld, hist in etf_rows:
        if not q:
            continue
        yz = youzi_score(s, q, f, yld, flows.get(s["code"]), inn_lines, out_lines, board_heat, hist)
        yz["annot"] = youzi_annot(s, q, f, hist, flows.get(s["code"]), mood, lhb)
        etf_yz.append((s, q, f, yld, yz))
    etf_yz.sort(key=lambda r: -r[4]["score"])
    etf_t1 = []
    for s, q, f, yld, hist in etf_rows:
        if not q:
            continue
        etf_t1.append((s, q, f, yld))
    etf_t1.sort(key=lambda r: -(r[2]["buy"] if r[2] else -999))

    sh = live.get("000001")
    cyb = live.get("399006")
    semi = live.get("512480") or live.get("159813")
    med = live.get("512010") or live.get("000933")
    comm = live.get("515050")
    hsi = live.get("HSI")
    nq = live.get("159941") or live.get("513100")

    tech_lines = TECH_LINES
    med_lines = MED_LINES
    doable, avoid = line_policy(inn_lines, out_lines)
    desk_lines = desk_lines_of(stocks, etfs)
    desk_avoid = avoid_for_desk(avoid, desk_lines)
    noise_out = [x for x in out_lines if x in NOISE_OUT]
    heat_map = flow_heat_map(inn, outf)

    # 统一走模块级实现，删掉原来这里的同名副本：过去主流程用嵌套版（ETF 主线不重映射）、
    # 单股分析页用模块版，同一只票两处结论会不一致。
    def line_status_of(s):
        return _line_status_of(s, doable, avoid)

    def heat_pts_of(st, line):
        return _heat_pts_of(st, line, heat_map)

    left_ranked = []
    for s, q, f, yld, hist in rows:
        if not q or not f:
            continue
        ls = left_setup(s, q, f, yld, hist, flows.get(s["code"]), idx_weak)
        if not ls:
            continue
        st, line = line_status_of(s)
        if ls["call"] == "可试仓" and st == "回避" and ls["kind"] != "游资":
            ls["call"] = "观察"
            ls["how"] = "超跌够了，板块资金还在出，只盯不抄"
        ls["line_st"] = st
        ls["line"] = line
        left_ranked.append((s, q, f, yld, ls))
    left_ranked.sort(key=lambda r: -r[4]["score"])
    left_trend = [x for x in left_ranked if x[4]["kind"] != "游资"]
    left_youzi = [x for x in left_ranked if x[4]["kind"] == "游资"]
    left_try = [x[0]["name"] for x in left_ranked if x[4]["call"] == "可试仓"]
    hist_by_code = {r[0]["code"]: r[4] for r in list(rows) + list(etf_rows)}
    # 左侧票用左侧那套止损（失败=破今日低），不套游资/打板的均线止损
    exits_left = {}
    for s, q, f, yld, ls in left_ranked:
        if ls.get("call") != "可试仓":
            continue
        ex = trade_exits(s, q, f, "左侧", "左侧", hist_by_code.get(s["code"]))
        if ex:
            exits_left[s["name"]] = ex
            exits_left[s["code"]] = ex

    pick_lines = []
    can_small = []
    watch = []
    no_buy_names = []
    for s, q, f, yld in ranked[:8]:
        call, why = name_call(s, q, f, yld)
        extra = []
        if f.get("orb_fake") or f.get("orb") == "假突":
            extra.append("假突")
        elif f.get("orb") == "未过开盘高":
            extra.append("未过开盘高")
        elif f.get("orb") == "过开盘高":
            extra.append("过开盘高")
        if f.get("yhl") == "破昨低":
            extra.append("破昨低")
        elif f.get("yhl") == "过昨高":
            extra.append("过昨高")
        if extra:
            why = why + "；" + "、".join(extra)
        ent = f.get("entry")
        ent_txt = f"；买点分{ent:.0f}" if ent is not None else ""
        pick_lines.append(f"{s['name']} {q['px']:.2f} {q['chg']:+.2f}% → **{call}**（{why}{ent_txt}）")
        if call == "可小仓":
            can_small.append(s["name"])
        elif call == "观察":
            watch.append(s["name"])
        else:
            no_buy_names.append(s["name"])

    overlay_lines = []
    for s, q, f, yld in ranked[:8]:
        call, why = name_call(s, q, f, yld)
        st, line = line_status_of(s)
        overlay_lines.append(f"{s['name']} 原判断{call} → 主线{line}/{st}")

    doable_hits = []
    for s, q, f, yld in ranked:
        st, line = line_status_of(s)
        if st != "可做" or not q:
            continue
        call, why = name_call(s, q, f, yld)
        doable_hits.append((s["name"], call, why, line, q))

    buy_today = "不买"
    buy_reason = []
    if shapes:
        buy_reason.append("大盘：" + "、".join(dict.fromkeys(shapes)))
    if inn_lines:
        buy_reason.append("资金流入：" + "、".join(inn_lines[:5]))
    if out_lines:
        buy_reason.append("资金流出：" + "、".join(out_lines[:4]))
    if doable:
        buy_reason.append("可做主线：" + "、".join(doable))
    else:
        buy_reason.append("没有资金确认且自选对得上的主线")
    if desk_avoid:
        buy_reason.append("回避主线：" + "、".join(desk_avoid[:8]) + "（主力净出，不是看涨幅热不热）")
    elif avoid:
        buy_reason.append("回避主线：无（自选对口线未净出；东财另有流出不连坐）")
    if noise_out:
        buy_reason.append("资金流出备注（不进回避闸）：" + "、".join(noise_out))
    if semi:
        buy_reason.append(f"半导体ETF {semi['chg']:+.2f}%")
    if med:
        buy_reason.append(f"医药ETF/指数 {med['chg']:+.2f}%")
    if comm:
        buy_reason.append(f"通信ETF {comm['chg']:+.2f}%")

    small_on_line = [x[0] for x in doable_hits if x[1] == "可小仓"]
    watch_on_line = [x[0] for x in doable_hits if x[1] == "观察"]
    screen_small_avoid = []
    for s, q, f, yld in ranked[:8]:
        call, _ = name_call(s, q, f, yld)
        st, line = line_status_of(s)
        if call == "可小仓" and st == "回避":
            screen_small_avoid.append(s["name"])
    orig_call = []
    if can_small:
        orig_call.append("原筛选可小仓：" + "、".join(can_small))
    if watch:
        orig_call.append("原筛选观察：" + "、".join(watch))
    if no_buy_names:
        orig_call.append("原筛选不买/不追：" + "、".join(no_buy_names))
    buy_reason = orig_call + buy_reason
    if small_on_line:
        buy_today = "可小仓"
        buy_reason.append("叠加后：原筛选买点落在今天资金主线上 → " + "、".join(small_on_line[:5]))
    elif can_small and screen_small_avoid:
        buy_today = "观察"
        buy_reason.append("叠加后：原筛选有买点（" + "、".join(can_small) + "），但今天资金主线不在这些票上，综合不开新仓")
    elif doable and watch_on_line:
        buy_today = "观察"
        buy_reason.append("叠加后：主线可做，筛出的对口票还不到买点：" + "、".join(watch_on_line[:5]))
    elif can_small:
        buy_today = "可小仓"
        buy_reason.append("叠加后：沿用原筛选买点：" + "、".join(can_small))
    elif doable:
        buy_today = "观察"
        buy_reason.append("叠加后：主线资金在进，自选对口不在筛选前列或已过热")
    else:
        buy_today = "不买" if not can_small else "观察"
        buy_reason.append("叠加后：资金主线与筛选前列不对齐")

    time_bits = timing_pred(now, shapes, doable, desk_avoid)
    buy_reason.extend(time_bits)

    key_names = ("沪电股份", "天孚通信", "新易盛", "千金药业")
    key_lines = []
    for s, q, f, yld, hist in rows:
        if s["name"] not in key_names or not q:
            continue
        vwap = q.get("vwap")
        pos = "均价上" if vwap and q["px"] >= vwap else "均价下"
        ma20 = f["ma20"] if f else None
        ma_pos = f"MA20 {ma20:.1f}" if ma20 else "MA20暂缺"
        call, why = name_call(s, q, f, yld) if f else ("观察", "缺日线")
        st, line = line_status_of(s)
        key_lines.append(
            f"{s['name']} {q['px']:.2f} {q['chg']:+.2f}% {pos} 量比{q['vol_ratio']:.2f} 低{q['low']:.2f} {ma_pos} → {call}（{why}）"
        )
    line_block = []
    if doable:
        line_block.append("可做：" + "、".join(doable))
    else:
        line_block.append("可做：无（资金主线与自选科技链劈叉或未确认）")
    if inn_lines:
        line_block.append("资金在进：" + "、".join(inn_lines[:5]) + "（自选若没有对口标的，只下结论不硬凑）")
    if desk_avoid:
        line_block.append("回避：" + "、".join(desk_avoid[:8]) + "（该线主力净出；涨幅热也可以回避）")
    else:
        line_block.append("回避：无（光通信/半导体等热门若资金在进，不因 PCB/元件/玻纤流出连坐）")
    if noise_out:
        line_block.append("旁支流出（不回避主线）：" + "、".join(noise_out))
    if doable_hits:
        line_block.append("主线上的筛选票：" + "；".join(f"{n} {c}" for n, c, _, _, _ in doable_hits[:8]))
    else:
        line_block.append("主线上的筛选票：无")

    tmin = now.hour * 60 + now.minute
    # 14:30 之后一律算尾盘，收盘后更不能开新仓。
    # 原来上界卡在 15:00，导致收盘后跑的报告把游资票重新标成「可小仓」，
    # 而单股分析页同一只票显示「尾盘不新开」，两处对不上。
    late_youzi = tmin >= 14 * 60 + 30
    after_close = tmin >= 15 * 60
    verdicts = {}
    daban_by_code = {}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        kind = stock_kind(s, q, hist)
        st, line = line_status_of(s)
        yz = yz_by_code.get(s["code"])
        db = daban_plan(s, q, f, hist, yz, st, line, mood, late_youzi, yld, zt_y, zt_t)
        if db.get("in_pool"):
            daban_by_code[s["code"]] = db
        if kind == "游资":
            call, why = verdict_youzi(s, q, f, yld, yz, st, mood, late_youzi)
        else:
            call, why = verdict_trend(s, q, f, yld, st)
        if db.get("call") == "可小仓" and call != "可小仓" and call not in ("不买", "不追"):
            call, why, kind = db["call"], db["why"], "打板"
        elif db.get("call") == "可小仓" and call == "可小仓":
            why = db["why"] + "；" + why
            kind = "打板"
        verdicts[s["code"]] = (call, why, kind, line, st)
    for s, q, f, yld, yz in etf_yz:
        call, why = verdict_etf(q, yz)
        verdicts[s["code"]] = (call, why, "ETF", "ETF", "-")

    t1_all, n_trend, n_youzi = [], 0, 0
    call_rank = {"可小仓": 0, "观察": 1, "不追": 2, "不买": 3}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        kind = stock_kind(s, q, hist)
        if kind == "游资":
            n_youzi += 1
        elif kind == "趋势":
            n_trend += 1
        else:
            continue
        call = (verdicts.get(s["code"]) or ("观察",))[0]
        t1_all.append((s, q, f, yld, hist, f["buy"] if f else -999, kind, call))
    t1_all.sort(key=lambda r: (call_rank.get(r[7], 9), -r[5]))
    etf_t1.sort(key=lambda r: (
        call_rank.get((verdicts.get(r[0]["code"]) or ("观察",))[0], 9),
        -(r[2]["buy"] if r[2] else -999),
    ))

    trend_ok, trend_no = [], []
    for s, q, f, yld in ranked:
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "趋势", "", ""))
        if is_limit_up(s["code"], q["chg"]) or q["chg"] >= 7:
            trend_no.append(f"{s['name']} 硬剔除/过热")
            continue
        if call == "可小仓" and kind != "打板":
            trend_ok.append((s["name"], q["px"], q["chg"], f["buy"], line, st, why, f.get("entry") or 0))
        elif name_call(s, q, f, yld)[0] == "可小仓" and st == "回避":
            trend_no.append(f"{s['name']} 表一可小仓但主线回避")
    youzi_ok, youzi_no = [], []
    if mood.get("phase") == "退潮":
        youzi_no.append("情绪退潮，7a高分也不开游资新仓")
    for s, q, f, yld, yz in yz_youzi:
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "游资", "", ""))
        if kind == "打板":
            continue
        if call == "可小仓":
            youzi_ok.append((s["name"], q["px"], q["chg"], yz["score"], line, st, why))
        elif yz["score"] >= 65 and call != "不追":
            youzi_no.append(f"{s['name']} 7a {yz['score']:.0f}分 {why}")
    if late_youzi:
        youzi_no.append(
            ("收盘后，游资/打板不新开，这份只当次日预案" if after_close
             else "14:30后游资只续不新开（7a分不改，仍列出备选）")
        )
    etf_ok = []
    for s, q, f, yld, yz in etf_yz:
        call, why = verdict_etf(q, yz)
        if call == "可小仓":
            etf_ok.append((s["name"], q["px"], q["chg"], yz["score"], why))
    left_ok = [x[0]["name"] + " " + x[4]["call"] for x in left_ranked if x[4]["call"] == "可试仓"]
    daban_ok = []
    exits_by_name = {}
    for s, q, f, yld, hist in list(rows) + list(etf_rows):
        if not q:
            continue
        v = verdicts.get(s["code"])
        kind = (v[2] if v else stock_kind(s, q, hist))
        db = daban_by_code.get(s["code"]) or {}
        if v:
            call, why, kind, line, st = v
            if kind == "打板" and call == "可小仓":
                daban_ok.append((s["name"], q["px"], q["chg"], db.get("score") or 0, line, st, why, s["code"]))
        ex = trade_exits(s, q, f, kind, db.get("setup") or "", hist)
        if ex:
            exits_by_name[s["name"]] = ex
            exits_by_name[s["code"]] = ex

    def xit(name, left=False):
        e = (exits_left.get(name) if left else None) or exits_by_name.get(name)
        if not e:
            return "-", "-"
        nd = int(e.get("nd") or 2)
        if e.get("sl_short") and e.get("tp_short"):
            return e["sl_short"], e["tp_short"]
        fmt = f"{{:.{nd}f}}"
        return fmt.format(e["sl"]), f"{fmt.format(e['tp1'])}/{fmt.format(e['tp2'])}"

    def line_best(block, key=None):
        best, seen = [], set()
        for r in sorted(block, key=key or (lambda x: -x[3])):
            line = r[4]
            if line in seen:
                continue
            seen.add(line)
            best.append(r)
        return best

    trend_best = line_best(trend_ok, key=lambda x: (-x[7], -x[3]))
    youzi_best = [] if late_youzi else line_best(youzi_ok)
    trend_let = [r for r in trend_ok if r not in trend_best]
    youzi_let = [r for r in youzi_ok if r not in youzi_best]
    pick_name, pick_why = None, None
    if trend_best:
        r = trend_best[0]
        pick_name, pick_why = r[0], f"趋势仓 表一{r[3]:.0f} 买点{r[7]:.0f} {r[4]}/{r[5]}"
    elif daban_ok:
        r = max(daban_ok, key=lambda x: x[3])
        pick_name, pick_why = r[0], f"打板仓 {daban_by_code.get(r[7], {}).get('setup') or '战法'} {r[3]:.0f} {r[4]}/{r[5]}"
    elif youzi_best:
        r = youzi_best[0]
        pick_name, pick_why = r[0], f"游资仓 7a {r[3]:.0f} {r[4]}/{r[5]}"
    elif etf_ok:
        r = max(etf_ok, key=lambda x: x[3])
        pick_name, pick_why = r[0], f"ETF分 {r[3]:.0f}"
    elif left_ok:
        pick_name, pick_why = left_ok[0].split()[0], "左侧轻仓，不替代右侧"
    buy_bits = []
    buy_bits.extend(x[0] for x in trend_ok)
    buy_bits.extend(x[0] for x in daban_ok)
    buy_bits.extend(x[0] for x in youzi_ok if x[0] not in buy_bits)
    buy_bits.extend(x[0] for x in etf_ok)
    buy_line = "、".join(buy_bits) if buy_bits else "没有。不开新仓"
    # 左侧可试仓本来就在第0节表里列着，却不进「可以买」，两处对不上。
    # 现在单列出来，标清是轻仓试不是过闸。
    left_names = [x.split()[0] for x in left_ok]
    if left_names:
        buy_line += f"；左侧轻仓试：{'、'.join(left_names[:4])}"
    # 第14节「综合结论」过去只用趋势池的 name_call 推，游资/打板/ETF 过闸的票抬不动它，
    # 于是同一份报告第0节说「可以买」、第14节说「不买」。现在统一用同一批过闸名单。
    if buy_bits:
        buy_today = "可小仓"
        buy_reason.append("综合结论与第0节同一套闸：过闸的是 " + "、".join(buy_bits))
    elif left_names:
        buy_today = "观察"
        buy_reason.append("综合结论：只有左侧可轻仓试，不算过闸，不开主仓")
    else:
        buy_today = "不买"
        buy_reason.append("综合结论：今天没有票过闸，不开新仓")

    def worth_pts(call, kind, st, line, tape, auc_call=None, ma_score=None):
        return _worth_pts(call, kind, st, line, tape, auc_call, ma_score, heat_map)

    def role_of(call, st):
        if call == "可小仓":
            return "可买"
        if call == "可试仓":
            return "轻仓备选"
        if call == "不追":
            return "不追"
        if call == "不买":
            return "不买"
        if st == "可做":
            return "备选(主线热)"
        if st == "回避":
            return "备选(板块冷)"
        return "备选"

    worth_by_code = {}

    def record_worth(code, name, kind, q, call, why, line, st, tape, tape_txt, auc_call=None, ma_score=None):
        if not code or code in worth_by_code or not q:
            return
        sc, htag, ap = worth_pts(call, kind, st, line, tape, auc_call, ma_score)
        worth_by_code[code] = {
            "name": name, "kind": kind, "px": q["px"], "chg": q["chg"],
            "call": call, "why": why, "line": line, "st": st,
            "heat": htag, "tape": tape or 0, "tape_txt": tape_txt,
            "score": sc, "role": role_of(call, st), "code": code,
            "auc": auc_call or "-", "auc_pts": ap, "ma": ma_score or 0,
        }

    for s, q, f, yld, hist in rows:
        if not q:
            continue
        v = verdicts.get(s["code"])
        if not v:
            continue
        call, why, kind, line, st = v
        yz = yz_by_code.get(s["code"])
        if kind == "游资":
            tape = (yz or {}).get("score") or 0
            tape_txt = f"7a {tape:.0f}"
        elif kind == "打板":
            tape = (daban_by_code.get(s["code"]) or {}).get("score") or 0
            tape_txt = f"打板{tape:.0f}"
        else:
            tape = (f or {}).get("entry") or 0
            tape_txt = f"买点{tape:.0f}"
        auc_call = ((f or {}).get("auction") or {}).get("call")
        ma_score = (f or {}).get("buy") or 0
        record_worth(s["code"], s["name"], kind, q, call, why, line, st, tape, tape_txt, auc_call, ma_score)
    for s, q, f, yld, yz in etf_yz:
        call, why, kind, _, _ = verdicts.get(s["code"], ("观察", "", "ETF", "ETF", "-"))
        line = ETF_LINE.get(s["name"], "ETF")
        st, line = line_status_of({"board": line if line != "ETF" else s.get("board")})
        tape = (yz or {}).get("score") or 0
        auc_call = ((f or {}).get("auction") or {}).get("call")
        ma_score = (f or {}).get("buy") or 0
        record_worth(s["code"], s["name"], "ETF", q, call, why, line, st, tape, f"ETF {tape:.0f}", auc_call, ma_score)
    # 左侧可试仓过去不进值分表，于是备选池里永远看不到它们
    for s, q, f, yld, ls in left_ranked:
        if ls.get("call") != "可试仓":
            continue
        w = worth_by_code.get(s["code"])
        if w and w["call"] == "可小仓":
            continue
        if w:
            w["call"] = "可试仓"
            w["role"] = "轻仓备选(左侧)"
            w["why"] = ls.get("how") or w["why"]
            w["tape_txt"] = f"左侧{ls.get('score') or 0:.0f}"
        else:
            st2, line2 = line_status_of(s)
            record_worth(
                s["code"], s["name"], "左侧", q, "可试仓", ls.get("how") or "左侧轻仓",
                line2, st2, ls.get("score") or 0, f"左侧{ls.get('score') or 0:.0f}",
                ((f or {}).get("auction") or {}).get("call"), (f or {}).get("buy") or 0,
            )
    worth_all = list(worth_by_code.values())
    worth_all.sort(key=lambda x: (-x["score"], x["name"]))
    for i, r in enumerate(worth_all, 1):
        r["rank"] = i
    top5 = [x for x in worth_all if x["call"] == "可小仓"][:5]
    alt_pool = [x for x in worth_all if x["call"] in ("观察", "可试仓")][:8]
    top5_line = "、".join(x["name"] for x in top5) if top5 else "没有"
    alt_line = "、".join(x["name"] for x in alt_pool) if alt_pool else "没有"

    # ---- 组合层：把「可以买」变成「买多少」，并挡住同一条主线的重复下注 ----
    risk_cfg = load_risk_cfg()
    exits_all = dict(exits_by_name)
    exits_all.update({k: v for k, v in exits_left.items()})
    pf_cands = []
    for x in top5:
        pf_cands.append({
            "code": x["code"], "name": x["name"], "kind": x["kind"],
            "line": x["line"], "px": x["px"], "score": x["score"], "call": x["call"],
        })
    for x in worth_all:
        if x["call"] == "可试仓" and all(c["code"] != x["code"] for c in pf_cands):
            pf_cands.append({
                "code": x["code"], "name": x["name"], "kind": x["kind"],
                "line": x["line"], "px": x["px"], "score": x["score"], "call": x["call"],
            })
    pf = portfolio_plan(pf_cands, {k: v for k, v in exits_all.items() if len(str(k)) == 6}, risk_cfg)

    # ---- 信号留档 + 复盘：这一轮判别写进日志，历史判别用已抓的日线回填收益 ----
    try:
        gate_state_save()
    except Exception:
        pass
    try:
        n_j = journal_record(worth_all, now)
    except Exception:
        n_j = 0
    try:
        review = journal_review(hist_by_code, 30, risk_cfg.get("cost_pct") or 0.1)
    except Exception:
        review = {}

    def wtxt(code):
        w = worth_by_code.get(code)
        if not w:
            return "-", "-"
        return f"{w['score']:.0f}", str(w.get("rank") or "-")

    lines = []
    lines.append(f"# 今日自选 {now.strftime('%Y-%m-%d %H:%M')} 北京")
    lines.append("")
    lines.append("## 0 能不能买")
    lines.append(f"**可以买：{buy_line}**")
    if pick_name:
        lines.append(f"**最适合买：{pick_name}（{pick_why}）**")
    else:
        lines.append("**最适合买：没有。不开新仓**")
    lines.append(f"**今日最值得买 TOP5：{top5_line}**")
    lines.append(f"**备选池（观察不进最值得买）：{alt_line}**")
    # 隔夜美股→自选关注（8点预案也用这一块；不改买点闸）
    ovn_picks = (ovn_scan or {}).get("dragons") or (ovn_scan or {}).get("picks") or []
    ovn_bias = (ovn_scan or {}).get("bias") or "-"
    if ovn_picks or (ovn_scan or {}).get("leaders") or (ovn_scan or {}).get("day_boards"):
        lines.append(f"### 隔夜美股→今日自选关注（{ovn_bias}；只盯不改买点闸）")
        lead_txt = "、".join(
            f"{t['theme']}({t['score']:+.2f})" for t in ((ovn_scan or {}).get("leaders") or [])[:4]
        ) or "无明显领涨"
        lines.append(f"- 隔夜偏好：**{ovn_bias}** — {(ovn_scan or {}).get('bias_why') or ''}")
        lines.append(f"- 领涨主题：{lead_txt}")
        day_b0 = (ovn_scan or {}).get("day_boards") or []
        if day_b0:
            lines.append(
                "- 次日关注板块："
                + "、".join(
                    f"{d['line']}({d['attitude']}/{d['money']})"
                    for d in day_b0 if d.get("in_desk")
                )
            )
        if (ovn_scan or {}).get("oil_note"):
            lines.append(f"- 商品：{(ovn_scan or {}).get('oil_note')}")
        lines.append("| 序 | 龙头预案 | 类型 | 板块 | 主题 | 板内 | 态度 |")
        lines.append("|---|---|---|---|---|---|---|")
        if not ovn_picks:
            lines.append("| - | 领涨主题不在自选池 | - | - | - | - | 只记录主题 |")
        else:
            for i, p in enumerate(ovn_picks[:8], 1):
                lines.append(
                    f"| {i} | {p['name']} | {p['kind']} | {p.get('line') or p.get('board')} | {p['theme']} | {p.get('rank') or '-'} | **{p['attitude']}** |"
                )
        avoid = (ovn_scan or {}).get("avoid") or []
        if avoid:
            lines.append(
                "- 隔夜偏弱映射（先回避高开）："
                + "、".join(f"{a['name']}({a['theme']})" for a in avoid[:6])
            )
        lines.append("- 说明：这是隔夜预案，开盘后仍以第0节买点闸+竞价+资金主线为准；隔夜强≠可买。")
    lines.append("| 仓 | 股票 | 价 | 今涨 | 买点 | 主线 | 为什么 | 止损 | 止盈 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    n0 = 0
    for name, px, chg, sc, line, st, why, ent in trend_ok:
        n0 += 1
        _, htag = heat_pts_of(st, line)
        sl, tp = xit(name)
        lines.append(f"| 趋势 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {why} | {sl} | {tp} |")
    for name, px, chg, sc, line, st, how, code in daban_ok:
        n0 += 1
        _, htag = heat_pts_of(st, line)
        sl, tp = xit(name)
        setup = (daban_by_code.get(code) or {}).get("setup") or "打板"
        lines.append(f"| 打板 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {setup}·{how} | {sl} | {tp} |")
    for name, px, chg, sc, line, st, how in youzi_ok:
        n0 += 1
        _, htag = heat_pts_of(st, line)
        sl, tp = xit(name)
        lines.append(f"| 游资 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {how} | {sl} | {tp} |")
    for name, px, chg, sc, how in etf_ok:
        n0 += 1
        ln = ETF_LINE.get(name, "ETF")
        st, line = line_status_of({"board": ln if ln != "ETF" else ""})
        _, htag = heat_pts_of(st, line)
        sl, tp = xit(name)
        lines.append(f"| ETF | {name} | {px:.3f} | {chg:+.2f}% | **可小仓** | {htag} | {how} | {sl} | {tp} |")
    for txt in left_ok[:4]:
        n0 += 1
        nm = txt.split()[0]
        sl, tp = xit(nm, left=True)
        lines.append(f"| 左侧 | {nm} | - | - | **可试仓** | 超跌 | 轻仓，不替代右侧；失败=破今日低 | {sl} | {tp} |")
    if not n0:
        lines.append("| - | 没有 | - | - | **不买** | - | 不开新仓 | - | - |")
    lines.append("- 止盈止损叠四套主流方法，只取有效位：结构（今低/昨低/10日低）、均线（游资MA5/10，趋势MA20）、ATR吊灯（近高往下2～2.2倍ATR）、固定风险（主板大约4.5%～6%）。贴身均价/均线离开不够远不当止损。止盈先看昨高、10日高（至少约1.5～2倍风险），再看20日高或涨停。距涨停不足2.5个点不设碎止盈。估算不是下单。观察票只是预案。")
    lines.append("### 今日最值得买 TOP5（按值分；过闸优先，均线分只作轻量参考）")
    lines.append("| 序 | 股票 | 仓 | 价 | 今涨 | 买点 | 竞价 | 主线热度 | 盘面 | 值分 | 角色 | 止损 | 止盈 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    if not top5:
        lines.append("| - | 没有 | - | - | - | - | - | - | - | - | - | - | - | 不开新仓 |")
    else:
        for i, r in enumerate(top5, 1):
            px = f"{r['px']:.3f}" if r["kind"] == "ETF" else f"{r['px']:.2f}"
            ap = r.get("auc_pts") or 0
            ap_txt = f"{ap:+.0f}" if ap else "0"
            sl, tp = xit(r["name"])
            lines.append(
                f"| {i} | {r['name']} | {r['kind']} | {px} | {r['chg']:+.2f}% | **{r['call']}** | {r.get('auc') or '-'}({ap_txt}) | {r['heat']} | {r['tape_txt']} | **{r['score']:.0f}** | {r['role']} | {sl} | {tp} | {r['why']} |"
            )
    lines.append("### 备选池（观察/可试仓；热度再高也不进最值得买；止盈止损是预案，未过闸不算仓）")
    lines.append("| 序 | 股票 | 仓 | 价 | 今涨 | 买点 | 竞价 | 主线热度 | 盘面 | 值分 | 角色 | 止损 | 止盈 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    if not alt_pool:
        lines.append("| - | 没有 | - | - | - | - | - | - | - | - | - | - | - | 暂无观察票 |")
    else:
        for i, r in enumerate(alt_pool, 1):
            px = f"{r['px']:.3f}" if r["kind"] == "ETF" else f"{r['px']:.2f}"
            ap = r.get("auc_pts") or 0
            ap_txt = f"{ap:+.0f}" if ap else "0"
            sl, tp = xit(r["name"])
            lines.append(
                f"| {i} | {r['name']} | {r['kind']} | {px} | {r['chg']:+.2f}% | **{r['call']}** | {r.get('auc') or '-'}({ap_txt}) | {r['heat']} | {r['tape_txt']} | **{r['score']:.0f}** | {r['role']} | {sl} | {tp} | {r['why']} |"
            )
    # ---- 0c 组合与仓位 ----
    cfgp = pf.get("cfg") or {}
    lines.append("### 0c 组合与仓位（先定买多少，再谈买哪只）")
    lines.append(
        f"口径：单笔风险 {cfgp.get('risk_pct')}% = {pf.get('risk_amt', 0):.0f}元（总资金{cfgp.get('capital')}元），"
        f"仓位=风险额÷止损距离；单票≤{cfgp.get('max_name_pct')}%、单主线≤{cfgp.get('max_line_pct')}%、"
        f"总仓≤{cfgp.get('max_total_pct')}%、同主线最多{cfgp.get('max_per_line')}只。"
        f"资金和比例改 risk_config.json。"
    )
    if pf.get("plan"):
        lines.append("| 序 | 股票 | 仓 | 主线 | 价 | 止损 | 止损距 | 建议股数 | 金额 | 占总资金 | 实际风险 | 净盈亏比 | 受限于 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(pf["plan"], 1):
            nd = 3 if r["kind"] == "ETF" else 2
            rr = f"{r['net_rr']:.1f}" if r.get("net_rr") else "-"
            lines.append(
                f"| {i} | {r['name']} | {r['kind']} | {r['line']} | {r['px']:.{nd}f} | {r['sl']:.{nd}f} | "
                f"{r['stop_pct']:+.1f}% | {r['shares']} | {r['amt']:.0f} | {r['pct']:.1f}% | "
                f"{r['risk_amt']:.0f}({r['risk_pct_real']:.2f}%) | {rr} | {r['binding']} |"
            )
        lines.append(
            f"- 计划总仓 {pf['used_pct']:.1f}%（{pf['used_total']:.0f}元）；主线分布："
            + "、".join(f"{k} {v:.0f}%" for k, v in sorted(pf["line_share"].items(), key=lambda x: -x[1]))
        )
    else:
        lines.append("- 没有可下仓位的标的（没过闸或缺止损位）。")
    for w in pf.get("warn") or []:
        lines.append(f"- **集中度警告**：{w}")
    if pf.get("skipped"):
        lines.append(
            "- 被组合层挡住：" + "；".join(f"{x['name']}（{x['reason']}）" for x in pf["skipped"][:6])
        )
    lines.append(
        "- 净盈亏比=(第一目标-现价-成本)÷(止损距离+成本)，已扣双边成本约{:.2f}%。".format(cfgp.get("cost_pct") or 0.1)
        + "低于1.2的直接不给仓位：那通常是没有有效结构位、止损只能用固定百分比顶上，说明这个位置本身不好，不是仓位大小的问题。"
    )

    # ---- 0d 信号复盘 ----
    rv = review or {}
    lines.append(f"### 0d 信号复盘（近{rv.get('days', 30)}日留档，判别是否真的有用）")
    if rv.get("n_eval"):
        lines.append(
            f"口径：留档 {rv['n_rec']} 条、已可评估 {rv['n_eval']} 条；"
            f"从信号当时价算到之后第1/3/5个交易日收盘，已扣成本{rv.get('cost_pct')}%。"
        )
        lines.append("| 判别 | 样本 | 次日胜率 | 次日均收 | 3日均收 | 5日均收 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rv.get("by_call") or []:
            a3 = f"{r['a3']:+.2f}%" if r["a3"] is not None else "-"
            a5 = f"{r['a5']:+.2f}%" if r["a5"] is not None else "-"
            lines.append(
                f"| **{r['key']}** | {r['n']} | {r['win']:.0f}% | {r['a1']:+.2f}% | {a3} | {a5} |"
            )
        by_kind = [r for r in (rv.get("by_kind") or []) if r["key"].startswith(("可小仓", "可试仓"))]
        if by_kind:
            lines.append("| 判别/战法 | 样本 | 次日胜率 | 次日均收 | 3日均收 | 5日均收 |")
            lines.append("|---|---|---|---|---|---|")
            for r in by_kind[:8]:
                a3 = f"{r['a3']:+.2f}%" if r["a3"] is not None else "-"
                a5 = f"{r['a5']:+.2f}%" if r["a5"] is not None else "-"
                lines.append(
                    f"| {r['key']} | {r['n']} | {r['win']:.0f}% | {r['a1']:+.2f}% | {a3} | {a5} |"
                )
        lines.append("- 看法：可小仓的次日胜率和均收要明显高于观察，否则闸没有区分度；某个战法长期为负就该关掉它，而不是继续调参。样本少于20条先别下结论。")
    else:
        lines.append(f"- 今天开始留档（本次新增 {n_j} 条）。要等隔一个交易日才有可评估样本，之后这里会出胜率表。")
    lines.append("- 复盘只用来改阈值，不参与今天的判别。")

    trend_watch = [f"{s['name']}" for s, q, f, yld in ranked if verdicts.get(s["code"], ("",))[0] == "观察"]
    yz_watch = [f"{s['name']}" for s, q, f, yld, yz in yz_youzi if verdicts.get(s["code"], ("",))[0] == "观察" and yz["score"] >= 60]
    if trend_watch:
        lines.append("- 趋势观察：" + "、".join(trend_watch[:8]))
    if yz_watch:
        lines.append("- 游资观察（7a尚可但总判没过）：" + "、".join(yz_watch[:6]))
    high_no = []
    for s, q, f, yld, hist, buy, kind, call in sorted(t1_all, key=lambda r: -r[5]):
        if call in ("不买", "不追") and buy >= 60:
            why = (verdicts.get(s["code"]) or ("", ""))[1]
            high_no.append(f"{s['name']} {buy:.0f}分 {call}（{why}）")
        if len(high_no) >= 5:
            break
    if high_no:
        lines.append("- 高分但不买：" + "；".join(high_no) + "。分高≠能买")
    lines.append("- 能不能买以第0节「可以买/买点」为准。TOP5只是可小仓里按值分谁更靠前，值分高不能推翻闸，也不能把出货票洗白。")
    lines.append("- 可以买=总闸过了才能开仓。TOP5只排可小仓（按值分）；观察/可试仓再热也只进备选池，不把TOP5凑满。值分：闸+主线热+盘面(趋势买点分/游资7a)×0.28+均线分×0.18+竞价。均线分只拉开能买里谁更稳，不能翻盘。")
    lines.append("- 值分去重：游资/打板/ETF 的盘面分里已含板块资金和竞价质量，值分里主线热度只按0.45计、竞价不再重复加（竞价列显示0即此意，判别仍照常用）；趋势用买点分，不含这两项，全额计。")
    lines.append("- 判别加了滞后带：刚过线（如7a 65～70）要连续两次达标才给可小仓；已在可小仓的，分数回落到60以上仍维持。降级、骗炮、回避、退潮立即生效，不等确认。")
    lines.append("- 打板仓（近7日涨停池）：只做一进二/弱转强/板内龙头回头。昨板质量取东财涨停池真值（封单额/封成比/开板次数/是否一字），昨一字板不打、昨烂板才算弱转强、龙回头必须是板内前列且缩量回踩均线。昨板今日整体亏钱（赚钱效应差）时打板不新开。打板分≥75才可小仓，不替代7a游资闸。")
    lines.append("- 竞价涨停/近板开后砸盘→不买（出货）。竞价质量已并入各战法盘面分；量比和换手都按时段归一（早盘成交前置，10:00的量比1.5不等于14:30的1.5）。")
    lines.append("- 表一看「买点」列：可小仓=能买，观察=盯着，不买/不追=不能买。分只是均线健康。")
    miss_q = [x["name"] for x in stocks + etfs if not live.get(x["code"])]
    if miss_q or FETCH_FAIL:
        bits = []
        if miss_q:
            bits.append("无行情（停牌/取不到）：" + "、".join(miss_q[:8]))
        if FETCH_FAIL:
            bits.append("取数失败：" + "、".join(f"{k}×{v}" for k, v in FETCH_FAIL.items()))
        lines.append("- **数据完整性**：" + "；".join(bits) + "。这些票的结论不可用，别当成「没信号」。")
    lines.append("")
    ovn_blk = MACRO.get("overnight_external") or {}
    lines.append("## 1 外盘隔夜")
    if MACRO.get("fed"):
        lines.append("- " + MACRO["fed"])
    for x in ovn_blk.get("policy") or []:
        lines.append("- 政策：" + x)
    for x in ovn_blk.get("markets") or []:
        lines.append("- 盘面：" + x)
    if ovn:
        lines.append("- 隔夜报价：" + "；".join(ovn[:14]))
        if len(ovn) > 14:
            lines.append("- 隔夜报价续：" + "；".join(ovn[14:]))
    if ext_lines:
        lines.append("- 今日映射：" + "；".join(ext_lines))
    if ovn_blk.get("direction"):
        lines.append("- 方向：" + ovn_blk["direction"])
    if ovn_blk.get("implication"):
        lines.append("- 对今天：" + ovn_blk["implication"])
    if not (MACRO.get("fed") or ovn or ext_lines):
        lines.append("- 外盘调研暂缺")
    lines.append("")
    lines.append("## 1b 隔夜映射")
    lines.append(
        "框架：美股指数 + 行业ETF领涨/领跌 + 黄金/白银/铜/原油 + 光通信美股(LITE/COHR)。"
        "先定次日国内关注板块，再从自选里筛龙头。只作预案，不进买点闸、不进表一分/7a。"
    )
    sc = ovn_scan or {}
    lines.append(f"- 隔夜偏好：**{sc.get('bias') or '-'}** — {sc.get('bias_why') or ''}")
    if sc.get("_from_cache"):
        lines.append("- 报价来源：缓存（Yahoo 实时未拉到，用上一份有效隔夜）")
    if sc.get("indices"):
        lines.append(
            "- 美股/亚太指数："
            + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in sc["indices"])
        )
    if sc.get("sectors"):
        top_s = sc["sectors"][:5]
        bot_s = list(reversed(sc["sectors"][-4:])) if len(sc["sectors"]) >= 4 else []
        lines.append("- 美股板块领涨：" + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in top_s))
        if bot_s:
            lines.append("- 美股板块领跌：" + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in bot_s))
    if sc.get("metals"):
        lines.append(
            "- 金属/原油/美元债："
            + "；".join(
                f"{r['name']} {r['last']:.2f}({r['chg']:+.2f}%)"
                if r["sym"] != "^TNX"
                else f"{r['name']} {r['last']:.3f}%({r['chg']:+.1f}bp)"
                for r in sc["metals"]
            )
        )
    if sc.get("related"):
        lines.append(
            "- 光通信美股："
            + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in sc["related"])
        )
    if sc.get("oil_note"):
        lines.append("- 商品提示：" + sc["oil_note"])
    lines.append("### 领涨主题")
    lines.append("| 序 | 领涨主题 | 态度 | 得分 | 隔夜依据 | 映射自选板块 |")
    lines.append("|---|---|---|---|---|---|")
    leads = sc.get("leaders") or []
    if not leads:
        lines.append("| - | 暂无明显领涨 | - | - | - | - |")
    else:
        for i, t in enumerate(leads, 1):
            lines.append(
                f"| {i} | **{t['theme']}** | {t.get('tag') or '-'} | {t['score']:+.2f} | {t['detail']} | {'/'.join(t['boards'])} |"
            )
    lines.append("### 次日关注板块")
    lines.append("| 序 | 国内板块 | 隔夜主题 | 态度 | A股资金 | 为什么 |")
    lines.append("|---|---|---|---|---|---|")
    day_b = sc.get("day_boards") or []
    shown_b = [d for d in day_b if d.get("in_desk")]
    if not shown_b:
        lines.append("| - | 领涨主题不在自选池 | - | 无 | - | 只记录主题，不加戏 |")
        off = [d for d in day_b if not d.get("in_desk")]
        if off:
            lines.append(
                "- 池外主题（不进自选关注）："
                + "、".join(f"{d['theme']}→{d['line']}" for d in off[:6])
            )
    else:
        for i, d in enumerate(shown_b, 1):
            lines.append(
                f"| {i} | **{d['line']}** | {d['theme']} | **{d['attitude']}** | {d['money']} | {d['why']} |"
            )
    lines.append("### 龙头预案")
    lines.append("| 序 | 推荐自选 | 类型 | 板块 | 主题 | 板内 | 态度 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    dragons = sc.get("dragons") or []
    if not dragons:
        lines.append("| - | 关注板块里暂未筛出龙头 | - | - | - | - | 无 | 先看板块，开盘后再筛 |")
    else:
        for i, p in enumerate(dragons, 1):
            lines.append(
                f"| {i} | {p['name']} | {p['kind']} | {p.get('line') or p.get('board')} | {p['theme']} | {p.get('rank') or '-'} | **{p['attitude']}** | {p.get('why') or p.get('detail') or ''} |"
            )
    avoids = sc.get("avoid") or []
    if avoids:
        lines.append(
            "- 隔夜偏弱、开盘慎追："
            + "、".join(f"{a['name']}({a['theme']}{a['score']:+.2f})" for a in avoids[:8])
        )
    lines.append("- 用法：早上8点先看本节定关注名单；9:30后用第0节买点+竞价+资金主线确认，隔夜推荐不能单独开仓。")
    lines.append("")
    dom = MACRO.get("domestic") or {}
    lines.append("## 2 国内政策")
    for x in dom.get("policy") or []:
        lines.append("- 政策：" + x)
    for x in dom.get("direction") or []:
        lines.append("- 方向：" + x)
    if dom.get("implication"):
        lines.append("- 对今天：" + dom["implication"])
    if not (dom.get("policy") or dom.get("direction")):
        lines.append("- 国内政策调研暂缺")
    lines.append("")
    lines.append("## 3 大盘")
    if idx_lines:
        lines.extend("- " + x for x in idx_lines)
    else:
        lines.append("- 指数暂缺")
    lines.append("")
    lines.append("## 4 板块资金")
    lines.append("口径：东财行业主力净流入（估算）。流入/流出只定主线热度，不单独开仓。")
    lines.append("### 板块资金流入")
    lines.append("| 序 | 板块 | 涨跌 | 主力净流入 | 领涨 |")
    lines.append("|---|---|---|---|---|")
    if inn:
        for i, row in enumerate(inn[:8], 1):
            p = parse_sector_line(row)
            chg = f"{p['chg']:+.2f}%" if p["chg"] is not None else "-"
            amt = f"{p['amt']:+.1f}亿" if p["amt"] is not None else "-"
            lines.append(f"| {i} | {p['name']} | {chg} | {amt} | {p['lead']} |")
    else:
        lines.append("| - | 东财主力接口未取到 | - | - | 改看自选ETF |")
    lines.append("### 板块资金流出")
    lines.append("| 序 | 板块 | 涨跌 | 主力净流入 |")
    lines.append("|---|---|---|---|")
    if outf:
        for i, row in enumerate(outf[:6], 1):
            p = parse_sector_line(row)
            chg = f"{p['chg']:+.2f}%" if p["chg"] is not None else "-"
            amt = f"{p['amt']:+.1f}亿" if p["amt"] is not None else "-"
            lines.append(f"| {i} | {p['name']} | {chg} | {amt} |")
    else:
        lines.append("| - | 暂无流出榜 | - | - |")
    lines.append("### 自选ETF对照")
    lines.append("| 序 | ETF | 价 | 今涨 |")
    lines.append("|---|---|---|---|")
    n_etf = 0
    for e in etfs:
        qe = live.get(e["code"])
        if not qe:
            continue
        n_etf += 1
        lines.append(f"| {n_etf} | {e['name']} | {qe['px']:.3f} | {qe['chg']:+.2f}% |")
    if not n_etf:
        lines.append("| - | 自选ETF报价暂缺 | - | - |")
    etf_heat = [x for x in ext_lines if any(k in x for k in ("ETF", "医药", "半导体", "通信", "医疗"))]
    if etf_heat:
        lines.append("- 相关ETF/指数：" + "；".join(etf_heat))
    lines.append("- 东财暗盘/板块资金是模型估算，不是交易所盘前成交。")
    lines.append("")
    lines.append("## 4b 集合竞价")
    lines.append("框架：开幅（高开/低开）+ 竞价量比（首分钟量÷20日均分钟量）+ 开后是否站开盘 + 高低位。主流量价：高开放量偏抢筹，高开缩量/破开盘偏骗炮，低开放量偏砸，低开翻红看承接。9:15-9:20可撤单噪声大，开盘价以9:25撮合为准；开后3-5分钟再验证。")
    auc_rank = {"抢筹强": 0, "承接关注": 1, "正常": 2, "砸盘弱": 3, "骗炮警惕": 4, "竞价缺": 5}
    auc_rows = []
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        a = (f or {}).get("auction") or auction_judge(s, q, f, hist, yld)
        auc_rows.append((s, q, a))
    auc_rows.sort(key=lambda r: (auc_rank.get(r[2].get("call"), 9), -(r[2].get("vr") or 0), -abs(r[2].get("gap") or 0)))
    lines.append("| 序 | 股票 | 竞价判断 | 开幅 | 竞价量比 | 量能 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|")
    if not auc_rows:
        lines.append("| - | 暂无 | - | - | - | - | - |")
    else:
        for i, (s, q, a) in enumerate(auc_rows, 1):
            gap = f"{a['gap']:+.2f}%" if a.get("gap") is not None else "-"
            vr = f"{a['vr']:.1f}" if a.get("vr") is not None else "-"
            lines.append(
                f"| {i} | {s['name']} | **{a.get('call') or '-'}** | {gap} | {vr} | {a.get('vol_cls') or '-'} | {a.get('why') or '-'} |"
            )
    strong = [s["name"] for s, q, a in auc_rows if a.get("call") == "抢筹强"]
    watch_a = [s["name"] for s, q, a in auc_rows if a.get("call") == "承接关注"]
    trap_a = [s["name"] for s, q, a in auc_rows if a.get("call") == "骗炮警惕"]
    weak_a = [s["name"] for s, q, a in auc_rows if a.get("call") == "砸盘弱"]
    if strong:
        lines.append("- 抢筹强：" + "、".join(strong[:10]))
    if watch_a:
        lines.append("- 承接关注：" + "、".join(watch_a[:10]))
    if trap_a:
        lines.append("- 骗炮警惕：" + "、".join(trap_a[:10]))
    if weak_a:
        lines.append("- 砸盘弱：" + "、".join(weak_a[:10]))
    lines.append("- 竞价多数只作环境层+值分轻量加减，不改表一分/7a。例外：竞价涨停/近板开后砸盘改买点闸为不买。抢筹强≠可买。值分加减：抢筹+3 / 承接+1 / 正常0 / 砸盘-2 / 骗炮-3。")
    lines.append("")
    def t1_ma_flag(q, f):
        if not f:
            return "-"
        if q["px"] > (f["ma20"] or 0) and q["px"] > (f["ma5"] or 0):
            return "多"
        if q["px"] > (f["ma20"] or 0):
            return "上20"
        return "弱"

    lines.append(f"## 5 个股一览（全池{len(t1_all)}只，趋势{n_trend}+游资{n_youzi}）")
    lines.append("买点=能不能买。分=均线健康。买点分=九列。值分=闸+主线热+盘面(趋势买点分/游资7a)×0.28+均线分×0.18+竞价±3。买点分不重复加。竞价列只展示判断，不单独改买点。表一买点与明细标的相同，只保留明细（含买点表多出来的竞价/值序/为什么）。")
    lines.append("### 表一明细（买点+均线九列）")
    lines.append("| 序 | 股票 | 买点 | 竞价 | 类型 | 现价 | 今涨 | 分 | 买点分 | 值分 | 值序 | 均线 | 量比 | 距20日高 | 斜率 | 回踩 | 相对强度 | 板块RS | 量价 | ORB | 昨高 | 布林 | 换手分位 | 仓位 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    if not t1_all:
        lines.append("| - | 表一暂无报价 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    else:
        n = 0
        for s, q, f, yld, hist, buy, kind, call in t1_all:
            n += 1
            dd = f"{f['dd']*100:+.1f}%" if f else "-"
            sc = f"{f['buy']:.0f}" if f else "-"
            vr = f"{q['vol_ratio']:.2f}" if q.get("vol_ratio") is not None else "-"
            sl = (f.get("slope_txt") if f else None) or "-"
            pb = (f.get("pullback") if f else None) or "-"
            rs = (f.get("rs_txt") if f else None) or "-"
            brs = (f.get("board_rs") if f else None) or "-"
            vp = (f.get("vp") if f else None) or "-"
            orb = (f.get("orb") if f else None) or "-"
            yhl = (f.get("yhl") if f else None) or "-"
            boll = (f.get("boll") if f else None) or "-"
            hsp = (f.get("hs_pct") if f else None) or "-"
            pos = (f.get("pos") if f else None) or "-"
            ent = f"{f['entry']:.0f}" if f and f.get("entry") is not None else "-"
            why = (verdicts.get(s["code"]) or ("", ""))[1] or "-"
            ws, wr = wtxt(s["code"])
            ac = ((f or {}).get("auction") or {}).get("call") or "-"
            lines.append(
                f"| {n} | {s['name']} | **{call}** | {ac} | {kind} | {q['px']:.2f} | {q['chg']:+.2f}% | {sc} | {ent} | **{ws}** | {wr} | {t1_ma_flag(q, f)} | {vr} | {dd} | {sl} | {pb} | {rs} | {brs} | {vp} | {orb} | {yhl} | {boll} | {hsp} | {pos} | {why} |"
            )
    lines.append("")
    lines.append(f"### 表一ETF（{len(etf_t1)}只）")
    lines.append("| 序 | ETF | 买点 | 现价 | 今涨 | 分 | 值分 | 值序 | 均线 | 量比 | 距20日高 | 斜率 | 相对强度 | ORB | 仓位 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    n = 0
    for s, q, f, yld in etf_t1:
        n += 1
        dd = f"{f['dd']*100:+.1f}%" if f else "-"
        sc = f"{f['buy']:.0f}" if f else "-"
        vr = f"{q['vol_ratio']:.2f}" if q.get("vol_ratio") is not None else "-"
        sl = (f.get("slope_txt") if f else None) or "-"
        rs = (f.get("rs_txt") if f else None) or "-"
        orb = (f.get("orb") if f else None) or "-"
        pos = (f.get("pos") if f else None) or "-"
        call = (verdicts.get(s["code"]) or ("观察",))[0]
        ws, wr = wtxt(s["code"])
        lines.append(
            f"| {n} | {s['name']} | **{call}** | {q['px']:.3f} | {q['chg']:+.2f}% | {sc} | **{ws}** | {wr} | {t1_ma_flag(q, f)} | {vr} | {dd} | {sl} | {rs} | {orb} | {pos} |"
        )
    if not etf_t1:
        lines.append("| - | ETF报价暂缺 | - | - | - | - | - | - | - | - | - | - | - | - | - |")
    lines.append("")
    lines.append("## 6 趋势复核")
    if pick_lines:
        lines.extend("- " + x for x in pick_lines)
    else:
        lines.append("- 无合格筛选结果")
    lines.append("")
    lines.append("## 7 游资打分")
    lines.append("因子（7项+追高罚）：板块资金0.10｜资金合成0.30（今主力+5日+涨幅同向，同一口径合并计一次）｜游资弹性0.18（小市值+归一量比+振幅）｜涨停空间0.10｜竞价质量0.12｜低位启动0.10｜承接质量0.10（分时均价+封单额/封成比+开板次数）。量比已按时段归一，早盘不再天然高分。")
    lines.append(mood.get("txt") or "情绪：暂缺")
    lines.append("换手/竞价量/弱转强/超大单/题材板/昨龙虎/连板/昨ZT溢价/板内排名/竞价额/换手市值是加列和环境，不进 7 因子分、不改怎么做。")

    def emit_yz_table(title, block):
        lines.append(title)
        lines.append("| 序 | 股票 | 类型 | 板块 | 7因子 | 资金/暗盘代理 | 弹性 | 空间 | 操作分 | 怎么做 | 换手 | 竞价量 | 弱转强 | 超大单 | 题材板 | 昨龙虎 | 连板 | 昨ZT溢价 | 板内排名 | 竞价额 | 换手市值 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        if not block:
            lines.append("| - | 无 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
            return
        n = 0
        for s, q, f, yld, yz in block:
            n += 1
            space = f"{yz['cap']:.0f}cm 余{yz['room']:.1f}"
            kind = yz.get("kind") or stock_kind(s, q)
            a = yz.get("annot") or {}
            lines.append(
                f"| {n} | {s['name']} | {kind} | {yz.get('board') or s.get('board')} | {yz['factor_line']} | {yz['flow_txt']} | {yz['elast_mark']} | {space} | **{yz['score']:.0f}** | {yz['how']} | {a.get('hs_txt','-')} | {a.get('auc_vol','-')} | {a.get('w2s','-')} | {a.get('xl','-')} | {a.get('theme','-')} | {a.get('lhb','-')} | {a.get('lian','-')} | {a.get('zt_prem','-')} | {a.get('board_rank','-')} | {a.get('auc_amt','-')} | {a.get('hs_fit','-')} |"
            )

    emit_yz_table(f"### 7a 游资（{len(yz_youzi)}只）", yz_youzi)
    lines.append("### 7a 门槛（未达标≠永远不值得买，只是游资仓今天不开）")
    lines.append("- **7a未达标**=7因子分<65。总判最多观察，不能当游资可小仓。趋势票走趋势闸，不看这道65分。值分高不能推翻闸。")
    lines.append("- 过65还要同时满足：非今涨停/非+7%过热、非昨跌停骗炮、非竞价涨停开后砸盘、情绪非退潮、how不含骗炮/见顶/涨停结束、主线非回避、换手≥5%或量比≥1.5、14:30前。少一条就停在观察。")
    lines.append("| 序 | 股票 | 7a | 买点 | 未进可买的原因 |")
    lines.append("|---|---|---|---|---|")
    n_miss = 0
    spotlight = {"天孚通信", "新易盛", "天融信", "国芳集团", "金健米业"}
    miss_rows = []
    for s, q, f, yld, yz in yz_youzi:
        call, why = (verdicts.get(s["code"]) or ("观察", ""))[:2]
        if call == "可小仓":
            continue
        miss_rows.append((s["name"] in spotlight, -(yz.get("score") or 0), s, q, yz, call, why))
    miss_rows.sort()
    for _, _, s, q, yz, call, why in miss_rows[:12]:
        n_miss += 1
        lines.append(f"| {n_miss} | {s['name']} | {yz['score']:.0f} | **{call}** | {why} |")
    if not n_miss:
        lines.append("| - | 游资仓均过闸或本池无游资 | - | - | - |")
    tf = next((x for x in yz_ranked if x[0]["name"] == "天孚通信"), None)
    xy = next((x for x in yz_ranked if x[0]["name"] == "新易盛"), None)
    if tf:
        s, q, f, yld, yz = tf
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "", "", ""))
        lines.append(f"- 天孚通信：7a {yz['score']:.0f}，买点 **{call}**（{why}）。盘面再强，主线回避或闸没过就不能进最值得买。")
    if xy:
        s, q, f, yld, yz = xy
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "", "", ""))
        if (yz.get("score") or 0) < 65:
            hint = "7a<65就是未达标，不能当游资可买"
        else:
            hint = "7a已过65，挡在门外的是总闸（回避/量能/追高等），不是分数不够"
        lines.append(f"- 新易盛：7a {yz['score']:.0f}，买点 **{call}**（{why}）。{hint}。")
    lines.append("")
    lines.append("## 8 左侧超跌")
    lines.append("进池/左侧分仍是原公式：乖离/RSI/距前高超跌 + 收阳或长下影止跌 + 放量承接。斐波那契只标注。趋势可试仓额外要求：均线走平、且 RSI拐头或缩量再放量或二探不破；大盘偏弱则趋势票只观察。游资左侧不要求均线走平。失败：破今日低或 ATR。类型列区分趋势/游资。")
    left_merged = left_ranked
    lines.append(f"### 8 左侧超跌（趋势{len(left_trend)}+游资{len(left_youzi)}，共{len(left_merged)}只）")
    lines.append("| 序 | 股票 | 类型 | 位置 | 斐波那契 | 止跌确认 | 左侧确认 | 主线 | 左侧分 | 判断 | 怎么做 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    if not left_merged:
        lines.append("| - | 本池暂无超跌 | - | - | - | - | - | - | - | - | - |")
    else:
        n = 0
        for s, q, f, yld, ls in left_merged:
            n += 1
            lines.append(
                f"| {n} | {s['name']} | {ls['kind']} | {ls['pos']} | {ls.get('fib') or '-'} | {ls['factor']} | {ls.get('confirm') or '-'} | {ls['line']}/{ls['line_st']} | **{ls['score']:.0f}** | **{ls['call']}** | {ls['how']} |"
            )
    lines.append("")
    lines.append("## 9 暗盘资金")
    lines.append("口径：个股今主力/超大单/5日主力代理（东财估算），不是交易所盘前暗盘成交。流入=今主力>0，流出=今主力<0。")
    yz_flow = sorted(yz_youzi, key=lambda r: -((r[4].get("main") or 0)))
    lines.append("### 暗盘资金流入")
    lines.append("| 序 | 股票 | 今涨 | 今主力 | 超大单 | 5日 | 价量同向 | 板块 | 竞价 | 7a | 买点 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    inn_yz = [x for x in yz_flow if (x[4].get("main") or 0) > 0][:10]
    if inn_yz:
        for i, (s, q, f, yld, yz) in enumerate(inn_yz, 1):
            call = (verdicts.get(s["code"]) or ("观察",))[0]
            lines.append(
                f"| {i} | {s['name']} | {q['chg']:+.2f}% | {yz.get('main',0)/1e8:+.1f}亿 | {yz.get('xlarge',0)/1e8:+.1f}亿 | {yz.get('main5',0)/1e8:+.1f}亿 | {yz.get('same_mark') or '-'} | {yz.get('sec_txt') or '-'} | {yz.get('auc_txt') or '-'} | **{yz['score']:.0f}** | **{call}** |"
            )
    else:
        lines.append("| - | 游资池暂无主力净流入 | - | - | - | - | - | - | - | - | - |")
    lines.append("### 暗盘资金流出")
    lines.append("| 序 | 股票 | 今涨 | 今主力 | 超大单 | 5日 | 价量同向 | 板块 | 竞价 | 7a | 买点 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    out_yz = [x for x in yz_flow if (x[4].get("main") or 0) < 0][:10]
    if out_yz:
        for i, (s, q, f, yld, yz) in enumerate(out_yz, 1):
            call = (verdicts.get(s["code"]) or ("观察",))[0]
            lines.append(
                f"| {i} | {s['name']} | {q['chg']:+.2f}% | {yz.get('main',0)/1e8:+.1f}亿 | {yz.get('xlarge',0)/1e8:+.1f}亿 | {yz.get('main5',0)/1e8:+.1f}亿 | {yz.get('same_mark') or '-'} | {yz.get('sec_txt') or '-'} | {yz.get('auc_txt') or '-'} | **{yz['score']:.0f}** | **{call}** |"
            )
    else:
        lines.append("| - | 游资池暂无主力净流出 | - | - | - | - | - | - | - | - | - |")
    lines.append("- 涨但主力出=出货嫌疑，7a会降怎么做；暗盘流入≠可买，仍过第0节闸。")
    lines.append("")
    lines.append("## 10 重点跟踪")
    if key_lines:
        lines.extend("- " + x for x in key_lines)
    else:
        lines.append("- 重点票报价暂缺")
    hot = [f"{s['name']} {q['chg']:+.2f}%" for s, q, *_ in rows if q and q["chg"] >= 7]
    ylds = [s["name"] for s, q, f, yld, hist in rows if yld]
    lines.append("- 不追：" + ("，".join(hot) if hot else "暂无+7%以上"))
    lines.append("- 昨跌停：" + ("，".join(ylds) + "（骗炮才不买，弱转强不一刀切）" if ylds else "无"))
    lines.append("### 近7日打板排名（专用战法，按打板分）")
    lines.append("- 因子：昨板质量（东财涨停池：封单额/封成比/开板次数/是否一字）、题材助攻、主线、市值20-80亿、换手8-22%、竞价3-7%且站住开盘、竞价量比、连板高度与市场最高板的距离、昨板今日赚钱效应、主力同向、情绪分档。今涨停仍不追；昨一字板不打。")
    lines.append("| 打板序 | 股票 | 战法 | 打板分 | 打板闸 | 总买点 | 近7日涨停 | 连板 | 今涨 | 说明 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    ranked_db = sorted(daban_by_code.items(), key=lambda kv: -kv[1].get("score", 0))
    n_zb = 0
    for code, db in ranked_db:
        row = next((x for x in rows if x[0]["code"] == code), None)
        if not row or not row[1]:
            continue
        s, q = row[0], row[1]
        n_zb += 1
        tot = (verdicts.get(code) or ("观察",))[0]
        dbc = db.get("call") or "观察"
        lian_txt = f"{db['n_lian']}连板" if db.get("n_lian", 0) >= 2 else ("昨首板" if db.get("n_lian") == 1 else "非连板")
        note = db.get("why") or db.get("factor") or "-"
        if dbc == "可小仓" and tot != "可小仓":
            note = (db.get("factor") or "") + f"；打板分够但总闸{tot}"
        lines.append(
            f"| {n_zb} | {s['name']} | {db.get('setup') or '-'} | **{db.get('score', 0):.0f}** | **{dbc}** | **{tot}** | {db.get('n7', 0)}次 | {lian_txt} | {q['chg']:+.2f}% | {note} |"
        )
    if not n_zb:
        lines.append("| - | 本池近7日无打板样本 | - | - | - | - | - | - | - |")
    if daban_ok:
        lines.append("- 打板可小仓：" + "、".join(x[0] for x in daban_ok))
    else:
        lines.append("- 打板可小仓：没有。宁缺毋滥。")
    lines.append("")
    lines.append("## 11 主线方向")
    lines.append("- 口径：板块冷/热看东财行业主力净流入，不是看涨幅热门。回避=该线自己资金净出。亨通跟CPO，和光模块放在光通信；东财通信线缆流出不把CPO打冷。元件/PCB流出不连坐光通信、半导体、液冷。三花=热管理。中材=玻纤。太极=半导体封测。")
    lines.extend("- " + x for x in line_block)
    lines.append("")
    lines.append("## 12 个股对照")
    if overlay_lines:
        lines.extend("- " + x for x in overlay_lines)
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 13 今日时点")
    lines.extend("- " + x for x in time_bits)
    lines.append("")
    lines.append("## 14 买点明细")
    lines.append(f"**综合结论：{buy_today}**")
    if pick_name:
        lines.append(f"**最适合买：{pick_name}（{pick_why}）**")
    else:
        lines.append("**最适合买：没有。不开新仓**")
    lines.append(f"**今日可以买：{buy_line}**")
    lines.append(f"**今日最值得买 TOP5：{top5_line}（仅可小仓，表同第0节）**")
    lines.append(f"**备选池：{alt_line}**")
    lines.append("| 仓 | 股票 | 价 | 今涨 | 分 | 主线 | 依据 | 适合度 | 结论 | 止损 | 止盈 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    n_buy_rows = 0

    def mark_row(name, layer_best):
        if pick_name == name:
            return "首选"
        if any(x[0] == name for x in layer_best):
            return "可买"
        return "同主线让"

    for name, px, chg, sc, line, st, why, ent in trend_ok:
        n_buy_rows += 1
        fit = mark_row(name, trend_best)
        sl, tp = xit(name)
        lines.append(f"| 趋势 | {name} | {px:.2f} | {chg:+.2f}% | 表一{sc:.0f}/买点{ent:.0f} | {line}/{st} | {why} | {fit} | **可小仓** | {sl} | {tp} |")
    for name, px, chg, sc, line, st, how, code in daban_ok:
        n_buy_rows += 1
        fit = "首选" if pick_name == name else "可买"
        sl, tp = xit(name)
        lines.append(f"| 打板 | {name} | {px:.2f} | {chg:+.2f}% | 打板{sc:.0f} | {line}/{st} | {how} | {fit} | **可小仓** | {sl} | {tp} |")
    for name, px, chg, sc, line, st, how in youzi_ok:
        n_buy_rows += 1
        fit = "尾盘不新开" if late_youzi else mark_row(name, youzi_best)
        sl, tp = xit(name)
        lines.append(f"| 游资 | {name} | {px:.2f} | {chg:+.2f}% | 7a {sc:.0f} | {line}/{st} | {how} | {fit} | **可小仓** | {sl} | {tp} |")
    for name, px, chg, sc, how in etf_ok:
        n_buy_rows += 1
        fit = "首选" if pick_name == name else "可买"
        sl, tp = xit(name)
        lines.append(f"| ETF | {name} | {px:.3f} | {chg:+.2f}% | ETF分 {sc:.0f} | ETF | {how} | {fit} | **可小仓** | {sl} | {tp} |")
    for txt in left_ok[:6]:
        n_buy_rows += 1
        nm = txt.split()[0]
        fit = "首选" if pick_name == nm else "轻仓备选"
        sl, tp = xit(nm)
        lines.append(f"| 左侧 | {txt} | - | - | 表三 | 超跌 | 轻仓试，不替代右侧 | {fit} | **可试仓** | {sl} | {tp} |")
    if not n_buy_rows:
        lines.append("| - | 没有同时满足分层条件的票 | - | - | - | - | 表一可小仓+主线未回避；或打板分≥75一进二/弱转强；或7a≥65；或表三可试仓 | - | **不买** | - | - |")
    lines.append("")
    lines.append("总判规则：趋势=表一可小仓且主线不是回避（同主线按买点分优先）；打板=近7日涨停池、一进二或弱转强、打板分≥75、未今涨停/未骗炮/未回避/非退潮；游资=7a≥65、未涨停/未骗炮、主线不是回避、情绪非退潮、换手≥5%或量比≥1.5；ETF=表一ETF对照、7因子可小仓且分≥60涨幅<5%；左侧=表三可试仓（轻仓）。最适合买：趋势可小仓 > 打板达标 > 游资达标 > ETF > 左侧。值分含均线分×0.18作参考，不替代闸。昨跌停看骗炮/弱转强，今涨停不追。止盈止损参考均线、前高、ATR吊灯和固定风险，不设不到1个点的碎止盈。估算不是下单。")
    if trend_let:
        lines.append("- 同主线趋势让出：" + "、".join(x[0] for x in trend_let))
    if youzi_let:
        lines.append("- 同主线游资让出：" + "、".join(x[0] for x in youzi_let))
    if late_youzi:
        lines.append("- 时点：14:30后游资不新开，只续仓；趋势仓不受影响")
    yz_top = [x for x in yz_youzi if x[4]["score"] >= 65 and x[1]["chg"] < 7 and not x[3]][:3]
    if yz_top:
        lines.append("- 游资仓参考（只看7a）：" + "、".join(f"{s['name']} {yz['score']:.0f}分 {yz['how']}" for s, q, f, yld, yz in yz_top))
    else:
        lines.append("- 游资仓：7a前排过热或不到分，不新开")
    lines.append("- 趋势仓：只看表一趋势池可小仓；游资不进表一买点，也不把7因子/回踩/斜率加进均线分")
    if mood.get("phase") == "退潮":
        lines.append("- 游资仓：情绪退潮，表二 7 因子高分也只看不追（分本身不改）")
    elif mood.get("phase") == "高潮":
        lines.append("- 游资仓：情绪高潮，可看跟风，仍不追高标；7因子分不改")
    if idx_weak_why:
        lines.append("- 表三大盘环境：" + "、".join(idx_weak_why) + " → 趋势左侧只观察，游资左侧不因此关掉")
    if left_try:
        lines.append("- 左侧试仓（表三，轻仓，不替代右侧）：" + "、".join(left_try[:6]))
    else:
        lines.append("- 左侧仓：超跌票未止跌或板块仍在出，只盯不抄")
    if trend_no[:4]:
        lines.append("- 趋势未过总判：" + "；".join(trend_no[:4]))
    if youzi_no[:4]:
        lines.append("- 游资未过总判：" + "；".join(youzi_no[:4]))
    for r in buy_reason:
        lines.append(f"- {r}")
    lines.append("- 不代下单、不要账号。")
    lines.append("")
    out = "\n".join(lines)
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")
    fn = os.path.join(ROOT, "reports", stamp + ".md")
    open(fn, "w", encoding="utf-8").write(out)
    meta = {
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "time_iso": now.isoformat(),
        "buy_today": buy_today,
        "pick_name": pick_name or "",
        "pick_why": pick_why or "",
        "doable": doable,
        "avoid": desk_avoid[:10],
        "avoid_note": "热门涨幅≠主力在进。回避只看该线净出，不把光通信/半导体和PCB绑在一起。",
        "can_small": can_small,
        "watch": watch,
        "small_on_line": small_on_line,
        "buy_names": buy_bits,
        "top5": [{"name": x["name"], "role": x["role"], "score": round(x["score"], 1), "heat": x["heat"],
                  "sl": (exits_by_name.get(x["name"]) or {}).get("sl"),
                  "tp": (lambda e: f"{e['tp1']}/{e['tp2']}" if e else None)(exits_by_name.get(x["name"]))} for x in top5],
        "alt_pool": [{"name": x["name"], "call": x["call"], "score": round(x["score"], 1),
                      "sl": (exits_by_name.get(x["name"]) or {}).get("sl"),
                      "tp": (lambda e: f"{e['tp1']}/{e['tp2']}" if e else None)(exits_by_name.get(x["name"]))} for x in alt_pool],
        "daban": [{"name": x[0], "score": x[3], "setup": (daban_by_code.get(x[7]) or {}).get("setup")} for x in daban_ok],
        "left_try": left_names,
        "portfolio": {
            "cfg": pf.get("cfg"),
            "used_pct": round(pf.get("used_pct") or 0, 1),
            "warn": pf.get("warn") or [],
            "line_share": {k: round(v, 1) for k, v in (pf.get("line_share") or {}).items()},
            "plan": [
                {"name": r["name"], "line": r["line"], "shares": r["shares"],
                 "amt": round(r["amt"]), "pct": round(r["pct"], 1),
                 "sl": r["sl"], "stop_pct": round(r["stop_pct"], 2),
                 "net_rr": round(r["net_rr"], 2) if r.get("net_rr") else None,
                 "binding": r["binding"]}
                for r in (pf.get("plan") or [])
            ],
            "skipped": [{"name": x["name"], "reason": x["reason"]} for x in (pf.get("skipped") or [])],
        },
        "review": review or {},
        "mood": {
            "phase": mood.get("phase"), "n_zt": mood.get("n_zt"), "n_dt": mood.get("n_dt"),
            "high": mood.get("high"), "n_lian": mood.get("n_lian"),
            "env": {k: mood.get("env", {}).get(k) for k in ("n", "prem", "adv", "green", "ok")},
        },
        "data_health": {"missing_quote": miss_q, "fetch_fail": FETCH_FAIL},
    }
    open(os.path.join(ROOT, "reports", "latest.json"), "w", encoding="utf-8").write(
        json.dumps(meta, ensure_ascii=False, indent=2)
    )
    print(out)
    print("\nSAVED", fn)
    html_fn, pdf_fn = write_report_html(out, stamp, now.strftime("%Y-%m-%d %H:%M"), open_view=True)
    print("SAVED", html_fn)
    if pdf_fn:
        print("SAVED", pdf_fn)
        print("双击这个PDF看表:", pdf_fn)
    else:
        print("浏览器打开:", html_fn)


def inline_md(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def cell_class(header, text):
    t = text.replace("**", "").strip()
    if header in ("今涨", "涨跌"):
        if t.startswith("+"):
            return "up"
        if t.startswith("-"):
            return "dn"
    keys = ("状态", "判断", "怎么做", "操作分", "买点", "角色", "竞价判断", "竞价", "价量同向", "流向")
    if header in keys or header in ("判断",):
        if any(k in t for k in ("可小仓", "可试仓", "可做", "可买", "抢筹强")):
            return "ok"
        if any(k in t for k in ("不买", "不追", "回避", "剔除", "骗炮", "见顶", "板块冷", "砸盘弱")):
            return "bad"
        if "观察" in t or "备选" in t or "承接" in t:
            return "watch"
    if header in ("分", "操作分", "左侧分", "值分") and t.replace(".", "", 1).isdigit():
        try:
            v = float(t)
            if v >= 65:
                return "ok"
            if v < 40:
                return "bad"
        except ValueError:
            pass
    return ""


def render_report_html(md, title="自选快照"):
    css = """
:root { --bg:#111110; --paper:transparent; --card:#1c1c1a; --line:rgba(242,241,236,.12);
  --ink:#f2f1ec; --muted:#9a9890; --gold:#c5c2b6; --navy:#111110; --ok:#6fba8d; --bad:#d67a7a; --watch:#c4b48a;
  --font:"Noto Sans SC","PingFang SC","Hiragino Sans GB",sans-serif;
  --display:"Noto Sans SC","PingFang SC",sans-serif;
  --mono:"IBM Plex Mono","Noto Sans SC",ui-monospace,monospace; }
* { box-sizing:border-box; }
html { -webkit-text-size-adjust:100%; }
body { margin:0; font-family:var(--font); color:var(--ink); line-height:1.65; letter-spacing:0;
  background:#111110; }
@keyframes rise { from { opacity:0; transform:translateY(6px); } to { opacity:1; transform:none; } }
nav.toc { position:sticky; top:0; z-index:40; display:flex; gap:6px; flex-wrap:nowrap;
  overflow-x:auto; -webkit-overflow-scrolling:touch; scrollbar-width:thin;
  background:rgba(17,17,16,.94); backdrop-filter:blur(16px); -webkit-backdrop-filter:blur(16px);
  border-bottom:1px solid rgba(242,241,236,.12);
  padding:calc(12px + env(safe-area-inset-top,0px)) 14px 12px; font-size:13px; }
nav.toc a { flex:0 0 auto; color:#9a9890; text-decoration:none; padding:8px 12px;
  border-radius:10px; white-space:nowrap; }
nav.toc a:hover { color:#111110; background:#f2f1ec; }
.page { min-height:100vh; max-width:1240px; margin:0 auto;
  padding:28px 20px calc(96px + env(safe-area-inset-bottom,0px)); animation:rise .4s ease; }
h1 { font-family:var(--display); font-size:clamp(26px,5.2vw,40px); letter-spacing:0;
  margin:4px 0 22px; font-weight:700; line-height:1.45; color:#f2f1ec; }
h1 .clock { display:block; margin-top:10px; font-family:var(--mono); font-size:16px; font-weight:500;
  letter-spacing:.08em; color:#c5c2b6; line-height:1.5; font-variant-numeric:tabular-nums; }
h2 { font-size:13px; letter-spacing:.08em; margin:36px 0 14px; padding-top:18px;
  border-top:1px solid rgba(242,241,236,.12); color:#9a9890; scroll-margin-top:62px; font-weight:600; }
h2#s0 { border:0; margin:2px 0 10px; color:#c5c2b6; font-size:13px; padding-top:0; }
.hero { color:#f2f1ec; background:#1c1c1a; border:1px solid rgba(242,241,236,.12);
  border-radius:14px; padding:20px 22px; margin:0 0 22px; }
.hero p { margin:0 0 10px; font-size:16px; font-weight:600; color:#f2f1ec; overflow-wrap:anywhere;
  letter-spacing:0; line-height:1.55; }
.hero p:last-child { margin-bottom:0; }
h3 { font-size:20px; margin:24px 0 12px; color:#f2f1ec; font-weight:700; line-height:1.4; letter-spacing:0; }
p, li { font-size:15px; overflow-wrap:anywhere; word-break:break-word; color:#d4d2ca; line-height:1.7; }
ul { padding-left:1.15em; }
.note { color:var(--muted); font-size:14px; margin:8px 0; }
.swipe { margin:10px 0 22px; }
.swipe .tip { display:none; font-size:12px; color:#9a9890; margin:0 0 8px; }
.wrap { overflow-x:auto; overflow-y:hidden; -webkit-overflow-scrolling:touch; overscroll-behavior-x:contain;
  border:1px solid rgba(242,241,236,.12); border-radius:14px; background:#1c1c1a; max-width:100%; }
table { border-collapse:collapse; min-width:100%; width:max-content; font-size:14px; }
th, td { border-bottom:1px solid rgba(242,241,236,.08); padding:12px 14px; text-align:left; white-space:nowrap; }
td.wrapcell, th.wrapcell { white-space:normal; min-width:120px; max-width:300px; }
th { background:#262624; color:#c5c2b6; font-weight:600; font-size:12px; }
tr:nth-child(even) td { background:rgba(255,255,255,.02); }
tr:hover td { background:rgba(242,241,236,.06); }
td { font-variant-numeric:tabular-nums; font-feature-settings:"tnum"; letter-spacing:.02em; }
.up { color:#d67a7a; font-weight:600; }
.dn { color:#6fba8d; font-weight:600; }
.ok { color:#6fba8d; font-weight:650; }
.bad { color:#d67a7a; font-weight:650; }
.watch { color:#c4b48a; font-weight:600; }
.hint { position:fixed; left:0; right:0; bottom:0; z-index:20;
  background:rgba(17,17,16,.94); color:#9a9890; border-top:1px solid rgba(242,241,236,.12);
  padding:12px 16px calc(12px + env(safe-area-inset-bottom,0px)); font-size:13px; }
@media (min-width:860px) {
  th { position:sticky; top:0; z-index:2; }
  th:nth-child(2), td:nth-child(2) { position:sticky; left:0; z-index:1; background:#1c1c1a; font-weight:600;
    box-shadow:2px 0 0 rgba(242,241,236,.12); }
  th:nth-child(2) { z-index:3; background:#262624; color:#c5c2b6; }
  tr:nth-child(even) td:nth-child(2) { background:#1e1e1c; }
}
@media (max-width:859px) {
  .swipe .tip { display:block; }
  nav.toc { font-size:11px; }
  table { font-size:12px; }
  th, td { padding:8px 9px; }
  td.wrapcell, th.wrapcell { max-width:46vw; min-width:96px; }
  h2 { scroll-margin-top:48px; }
  .hero p { font-size:15px; }
  .hint { display:none; }
  .page { padding-bottom:24px; }
}
@media print {
  @page { size: A3 landscape; margin: 8mm; }
  nav, .hint, .swipe .tip { display:none; }
  .wrap { overflow:visible; border:none; }
  table { font-size:10px; min-width:0; width:100%; }
  th, td { white-space:normal; }
  th, th:nth-child(2), td:nth-child(2) { position:static; box-shadow:none; }
  body, .page { background:#fff; color:#111; }
}
"""
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">',
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">',
        f"<title>{html.escape(title)}</title>",
        '<link rel="preconnect" href="https://fonts.googleapis.com">',
        '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>',
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Noto+Sans+SC:wght@400;500;700&display=swap" rel="stylesheet">',
        f"<style>{css}</style></head><body>",
        "<nav class=toc>",
        "<a href='#s0'>0 能不能买</a>",
        "<a href='#s1'>1 外盘隔夜</a>",
        "<a href='#s1b'>1b 隔夜映射</a>",
        "<a href='#s2'>2 国内政策</a>",
        "<a href='#s3'>3 大盘</a>",
        "<a href='#s4'>4 板块资金</a>",
        "<a href='#s4b'>4b 集合竞价</a>",
        "<a href='#s5'>5 个股一览</a>",
        "<a href='#s6'>6 趋势复核</a>",
        "<a href='#s7'>7 游资打分</a>",
        "<a href='#s8'>8 左侧超跌</a>",
        "<a href='#s9'>9 暗盘资金</a>",
        "<a href='#s10'>10 重点跟踪</a>",
        "<a href='#s11'>11 主线方向</a>",
        "<a href='#s12'>12 个股对照</a>",
        "<a href='#s13'>13 今日时点</a>",
        "<a href='#s14'>14 买点明细</a>",
        "</nav><div class=page>",
    ]
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    hero_open = False
    pending_hero = False
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue
        if hero_open and not line.startswith("**"):
            parts.append("</div>")
            hero_open = False
        if line.startswith("# "):
            title = line[2:].strip()
            m = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", title)
            if m:
                clock = f"{m.group(1)} {m.group(2)}"
                rest = (title[:m.start()] + title[m.end():]).replace("北京", "").strip()
                rest = re.sub(r"\s{2,}", " ", rest)
                parts.append(
                    f"<h1>{inline_md(rest)} <span class=clock>{html.escape(clock)} 北京</span></h1>"
                )
            else:
                parts.append(f"<h1>{inline_md(title)}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            title_txt = line[3:].strip()
            m = re.match(r"^(\d+[a-z]?)", title_txt)
            hid = f"s{m.group(1)}" if m else ""
            parts.append(f"<h2 id='{hid}'>{inline_md(title_txt)}</h2>")
            pending_hero = hid == "s0"
            i += 1
            continue
        if line.startswith("### "):
            pending_hero = False
            parts.append(f"<h3>{inline_md(line[4:])}</h3>")
            i += 1
            continue
        if line.startswith("|"):
            pending_hero = False
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            if not rows:
                continue
            def split_row(r):
                cells = [c.strip() for c in r.strip().strip("|").split("|")]
                return cells
            headers = split_row(rows[0])
            body = []
            for r in rows[1:]:
                cells = split_row(r)
                if cells and all(set(c.replace(":", "")) <= {"-", ""} for c in cells):
                    continue
                body.append(cells)
            wrap_headers = {"7因子", "资金/暗盘代理", "怎么做", "止跌确认", "左侧确认", "仓位", "为什么", "依据", "主线热度", "主线", "位置", "斐波那契", "未进可买的原因", "说明", "领涨", "板块", "战法", "止损", "止盈"}
            parts.append("<div class=swipe><div class=tip>宽表 · 左右滑动看全列</div><div class=wrap><table><thead><tr>")
            for h in headers:
                cls = " class=wrapcell" if h in wrap_headers else ""
                parts.append(f"<th{cls}>{inline_md(h)}</th>")
            parts.append("</tr></thead><tbody>")
            for cells in body:
                parts.append("<tr>")
                for j, c in enumerate(cells):
                    h = headers[j] if j < len(headers) else ""
                    cls = cell_class(h, c)
                    extra = "wrapcell" if h in wrap_headers else ""
                    classes = " ".join(x for x in (cls, extra) if x)
                    attr = f" class='{classes}'" if classes else ""
                    parts.append(f"<td{attr}>{inline_md(c)}</td>")
                parts.append("</tr>")
            parts.append("</tbody></table></div></div>")
            continue
        if line.startswith("- "):
            pending_hero = False
            parts.append("<ul>")
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                parts.append(f"<li>{inline_md(lines[i].lstrip()[2:])}</li>")
                i += 1
            parts.append("</ul>")
            continue
        if pending_hero and line.startswith("**"):
            parts.append("<div class=hero>")
            hero_open = True
            pending_hero = False
        parts.append(f"<p class=note>{inline_md(line)}</p>")
        i += 1
    if hero_open:
        parts.append("</div>")
    parts.append("</div>")
    parts.append("<div class=hint>左右滑动看全列 · 游资破均价 / 趋势看关键低</div>")
    parts.append("<script>document.documentElement.style.setProperty('--t', Date.now());</script>")
    parts.append("</body></html>")
    return "".join(parts)


def chrome_bin():
    for p in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
              "/usr/local/bin/google-chrome", "google-chrome", "chrome"):
        if os.path.isfile(p) or p in ("google-chrome", "chrome"):
            if os.path.isfile(p):
                return p
    return None


def html_to_pdf(html_fn, pdf_fn):
    chrome = chrome_bin()
    if not chrome:
        return None
    uri = "file://" + os.path.abspath(html_fn)
    cmd = [
        chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--no-pdf-header-footer",
        f"--print-to-pdf={os.path.abspath(pdf_fn)}", uri,
    ]
    try:
        subprocess.run(cmd, check=True, timeout=60, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return pdf_fn if os.path.isfile(pdf_fn) and os.path.getsize(pdf_fn) > 1000 else None
    except Exception:
        return None


def write_report_html(md, stamp, clock, open_view=False):
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    html_fn = os.path.join(ROOT, "reports", stamp + ".html")
    latest_html = os.path.join(ROOT, "reports", "latest.html")
    easy_html = os.path.join(ROOT, "自选快照.html")
    root_html = os.path.join(os.path.dirname(ROOT), "自选快照.html")
    body = render_report_html(md, f"今日自选 {clock} 北京")
    for p in (html_fn, latest_html, easy_html):
        open(p, "w", encoding="utf-8").write(body)
    if os.path.dirname(root_html) != ROOT:
        try:
            open(root_html, "w", encoding="utf-8").write(body)
        except Exception:
            pass
    pdf_fn = os.path.join(ROOT, "reports", stamp + ".pdf")
    latest_pdf = os.path.join(ROOT, "reports", "latest.pdf")
    easy_pdf = os.path.join(ROOT, "自选快照.pdf")
    made = html_to_pdf(html_fn, pdf_fn)
    if made:
        for p in (latest_pdf, easy_pdf):
            try:
                open(p, "wb").write(open(pdf_fn, "rb").read())
            except Exception:
                pass
        root_pdf = os.path.join(os.path.dirname(ROOT), "自选快照.pdf")
        if os.path.dirname(root_pdf) != ROOT:
            try:
                open(root_pdf, "wb").write(open(pdf_fn, "rb").read())
            except Exception:
                pass
    if open_view:
        view = made or html_fn
        try:
            webbrowser.open("file://" + os.path.abspath(view))
        except Exception:
            pass
    return html_fn, made


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] in ("--html", "--pdf"):
        src = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "reports", "20260917_1504.md")
        md = open(src, encoding="utf-8").read()
        stamp = os.path.splitext(os.path.basename(src))[0]
        clock = stamp
        m = re.search(r"自选快照\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})", md)
        if m:
            clock = m.group(1)
        html_fn, pdf_fn = write_report_html(md, stamp, clock, open_view=False)
        print("SAVED", html_fn)
        if pdf_fn:
            print("SAVED", pdf_fn)
        else:
            print("PDF未生成：本机装 Chrome 后再跑 --pdf")
    else:
        main()

