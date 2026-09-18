#!/usr/bin/env python3
"""Hourly watchlist: external + index + sector flow + MA/volume screen + buy/no-buy."""
import json, urllib.request, datetime, os, sys, html, re, subprocess, webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.abspath(__file__))
WL = json.load(open(os.path.join(ROOT, "watchlist.json"), encoding="utf-8"))
try:
    MACRO = json.load(open(os.path.join(ROOT, "macro_notes.json"), encoding="utf-8"))
except Exception:
    MACRO = {}


def http(url, gbk=False, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    })
    b = urllib.request.urlopen(req, timeout=timeout).read()
    return b.decode("gbk", "replace") if gbk else json.loads(b)


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
            out[p[2]] = {
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
            }
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


def http_json(url, timeout=12, referer="https://gu.qq.com/"):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": referer,
    })
    b = urllib.request.urlopen(req, timeout=timeout).read()
    return json.loads(b.decode("utf-8", "replace"))


def tencent_daily(s, n=160):
    """A股前复权日K。东财 kline 盘后常空，腾讯作主源。"""
    code = tencent_symbol(s)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{n},qfq"
    d = http_json(url, timeout=10)
    data = ((d.get("data") or {}).get(code) or {})
    rows = data.get("qfqday") or data.get("day") or []
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
    ysym = s["code"] + (".SS" if s.get("market") == "sh" else ".SZ")
    try:
        yb = yahoo_hist(ysym)
        if yb:
            return yb
    except Exception:
        pass
    return bars or []


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


def is_limit_up(code, chg):
    return chg >= (19.5 if is_20cm(code) else 9.5)


def yday_limit_down(code, hist):
    done = strip_today(hist) if hist else []
    if not done or len(done) < 2:
        return False
    prev, prev2 = done[-1][4], done[-2][4]
    if prev2 <= 0:
        return False
    chg = prev / prev2 - 1
    return chg <= (-0.195 if is_20cm(code) else -0.095)


def yday_dt_shape(q, yld):
    """昨跌停次日：trap骗炮 / turn弱转强 / weak续弱。不再一刀切不买。"""
    if not yld or not q or not q.get("prev"):
        return None
    o, p, prev = q["open"], q["px"], q["prev"]
    h = q["high"]
    vwap = q.get("vwap")
    gap = (o / prev - 1) * 100
    chg = q["chg"]
    if gap >= 0.5 and p < o:
        return "trap"
    if gap <= -0.15 and h > prev and p < o:
        return "trap"
    if chg > 0 and (vwap is None or p >= vwap):
        return "turn"
    if chg > 0:
        return "turn"
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


def score_row(hist, live):
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
    if live_chg >= 9.5:
        chase_pen += 30
    elif live_chg >= 5:
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
    9:15-9:20可撤单噪声大；开盘价=9:25撮合结果。不进表一分、不进8因子。"""
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
    # 一字/近涨停开盘
    if is_limit_up(s["code"], gap) or gap >= 9.5:
        if vol_cls in ("缩量", "温和", "不明") or (vr is not None and vr < 2):
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
        "orb_fake": False,
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
        stop = min(q["low"], px - atr)
        if atrp >= 4:
            out["pos"] = f"轻仓 ATR{atrp:.1f}% 破{stop:.2f}"
        elif atrp >= 2:
            out["pos"] = f"常规 ATR{atrp:.1f}% 破{stop:.2f}"
        else:
            out["pos"] = f"可略大 ATR{atrp:.1f}% 破{stop:.2f}"
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
TREND_BOARDS = {"中药", "创新药", "PCB", "有色", "光伏", "服务器", "汽车", "电网", "电力", "医疗", "机器人", "热管理", "半导体"}
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


def stock_kind(s, q, hist=None):
    """先点名/板块，再叠加券商盘面条件，提高游资入池比重。不改表一均线分、不改8因子。"""
    if s.get("asset") == "etf" or "ETF" in (s.get("name") or ""):
        return "ETF"
    if s["name"] in TREND_NAMES:
        return "趋势"
    if s.get("board") in TREND_BOARDS:
        return "趋势"
    if s.get("board") in YOUZI_BOARDS:
        return "游资"
    if is_20cm(s["code"]):
        return "游资"
    hits = youzi_tape_hits(s, q, hist)
    if len(hits) >= 2:
        return "游资"
    yi = mcap_yi(q)
    hs = (q.get("turnover") if q else None) or 0
    amp = (q.get("amp") if q else 0) or 0
    vr = (q.get("vol_ratio") if q else 0) or 0
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
    """8因子游资分。不改表一正常打分。"""
    chg = q["chg"] if q else 0
    vr = q["vol_ratio"] if q else 0
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
    cap = 20.0 if is_20cm(s["code"]) else 10.0
    room = cap - chg
    s_room = clip(room / cap * 100, 0, 100)
    room_mark = "见顶" if room <= 1 else ("紧" if room <= 3 else "足")

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

    # 8 追高罚：涨停、+10%、量比爆了还顶
    chase = 0
    chase_bits = []
    if is_limit_up(s["code"], chg) or chg >= 9.8:
        chase += 40
        chase_bits.append("涨停/+10%")
    elif chg >= 7:
        chase += 22
        chase_bits.append("+7%罚")
    elif chg >= 5:
        chase += 12
        chase_bits.append("+5%罚")
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

    score = (
        0.10 * s_sec + 0.22 * s_main_td + 0.10 * s_main5 + 0.15 * s_same
        + 0.18 * s_elast + 0.08 * s_room + 0.08 * s_auc + 0.09 * s_low - chase
    )

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
    elif is_limit_up(s["code"], chg):
        how = "涨停，结束/观察"
    elif chg >= 9.5:
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
        f"板块{sec_mark}；{main5_mark}；涨幅{same_mark}；弹性{elast_mark}；"
        f"空间{room_mark}；竞价{auc_mark}；低位{low_mark}；追高{chase_mark}"
    )
    return {
        "score": score, "kind": stock_kind(s, q, hist), "how": how, "board": board,
        "flow_txt": flow_txt + " " + same_txt,
        "sec_txt": sec_txt, "auc_txt": auc_txt, "factor_line": factor_line,
        "elast": s_elast, "elast_mark": elast_mark, "room": room, "cap": cap,
        "main": main, "main5": main5, "main_pct": (flow or {}).get("main_pct") or 0,
        "marks": {
            "板块资金": sec_mark, "主力5日": main5_mark, "资金涨幅同向": same_mark,
            "游资弹性": elast_mark, "涨停空间": room_mark, "竞价质量": auc_mark,
            "低位启动": low_mark, "追高罚": chase_mark,
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


def _yahoo_bar(sym):
    """Return (last, prev, chg_pct) or None."""
    try:
        data = http(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=10d",
            timeout=8,
        )
        res = data["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        closes = [c for c in q["close"] if c is not None]
        if len(closes) < 2:
            return None
        last, prev = closes[-1], closes[-2]
        return last, prev, (last / prev - 1) * 100
    except Exception:
        return None


# 美股领涨主题 → A股自选 board（watchlist.json 的 board 字段）
US_THEME_MAP = [
    {
        "theme": "半导体/算力硬件",
        "syms": [("^SOX", "费城半导体"), ("SMH", "半导体ETF"), ("XLK", "科技ETF")],
        "boards": ["半导体", "电子化学品", "元件", "服务器", "PCB"],
        "weight": 1.0,
    },
    {
        "theme": "光通信/CPO",
        "syms": [("LITE", "Lumentum"), ("COHR", "Coherent"), ("SMH", "半导体ETF")],
        "boards": ["光模块", "光纤"],
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
        "theme": "汽车链",
        "syms": [("XLY", "可选消费")],
        "boards": ["汽车"],
        "weight": 0.75,
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
    leaders = [t for t in themes if t["score"] >= 0.4][:5]
    laggers = [t for t in themes if t["score"] <= -0.4]
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
    board_names = {}
    for s in stocks:
        board_names.setdefault(s.get("board") or "", []).append(s["name"])
    etf_by_line = {}
    for e in etfs:
        ln = ETF_LINE.get(e["name"], "")
        if ln:
            etf_by_line.setdefault(ln, []).append(e["name"])

    picks = []
    seen = set()
    for t in leaders:
        for b in t["boards"]:
            for name in board_names.get(b, []):
                if name in seen:
                    continue
                seen.add(name)
                attitude = "优先盯" if t["score"] >= 1.2 else "可关注"
                picks.append({
                    "name": name, "board": b, "theme": t["theme"],
                    "score": t["score"], "detail": t["detail"],
                    "attitude": attitude, "kind": "股票",
                })
            # ETF 对照
            line = BOARD_LINE.get(b, b)
            for en in etf_by_line.get(line, []):
                if en in seen:
                    continue
                seen.add(en)
                picks.append({
                    "name": en, "board": line, "theme": t["theme"],
                    "score": t["score"] * 0.95, "detail": t["detail"],
                    "attitude": "ETF对照", "kind": "ETF",
                })
    picks.sort(key=lambda p: (-p["score"], p["name"]))
    picks = picks[:12]

    avoid = []
    for t in laggers[:4]:
        for b in t["boards"]:
            for name in board_names.get(b, [])[:3]:
                avoid.append({
                    "name": name, "board": b, "theme": t["theme"],
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


def overnight_quotes():
    """兼容旧调用：返回隔夜报价字符串列表。"""
    return overnight_scan().get("summary") or []


ETF_LINE = {
    "港股创新药ETF": "创新药", "创新药ETF东财": "创新药", "粮食ETF": "农业",
    "科创半导体ETF": "半导体", "科创50ETF东财": "半导体",
    "恒生科技ETF": "电子/科技", "有色金属ETF": "有色",
}

BOARD_LINE = {
    "液冷": "算力液冷", "热管理": "算力液冷", "服务器": "算力硬件",
    "光模块": "光通信", "光纤": "光通信", "PCB": "PCB",
    "半导体": "半导体", "电子化学品": "半导体", "元件": "电子元件",
    "消费电子": "消费电子",
    "医药游资": "游资医药", "医药": "医药", "中药": "中药",
    "创新药": "创新药", "医疗": "医疗",
    "汽车": "汽车", "有色": "有色", "超硬材料": "有色",
    "农业": "农业", "种业": "农业", "粮食": "农业",
    "电网": "电网", "电网设备": "电网", "电力": "电力", "光伏": "光伏",
    "传媒": "传媒", "广告": "传媒", "消费": "消费", "零售": "消费",
    "化工": "化工", "软件": "软件", "机器人": "机器人",
}

BOARD_BENCH = {
    "光模块": "515050", "光纤": "515050",
    "PCB": "512480", "半导体": "512480", "电子化学品": "512480",
    "液冷": "512480", "热管理": "512480", "服务器": "512480", "元件": "512480",
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
    "光纤": ["通信设备", "通信服务", "通信"],
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
    ("半导体", "半导体"), ("元件", "电子元件"), ("电子", "电子/科技"),
    ("有色金属", "有色"), ("工业金属", "有色"),
    ("通信", "光通信"), ("计算机", "软件"), ("软件", "软件"),
    ("国防", "军工"), ("军工", "军工"),
    ("农业", "农业"), ("种业", "农业"),
    ("电网", "电网"), ("电力", "电力"),
]


def sector_token(row):
    return (row or "").split()[0] if row else ""


def line_of_board(board):
    return BOARD_LINE.get(board, board or "其他")


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


def flow_heat_map(inn, outf):
    """当日板块主力热度。只给排名用，不进表一分、不进8因子。"""
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


def market_mood():
    """全市场涨停生态。只作表二环境层，不进 8 因子分。"""
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
            lim = 19.5 if code.startswith(("300", "301", "688")) else 9.5
            if chg >= lim:
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
            lim = -19.5 if code.startswith(("300", "301", "688")) else -9.5
            if chg <= lim:
                dt.append({"code": code, "name": name, "chg": chg, "hy": str(x.get("f100") or "")})
                page_dt += 1
        if page_dt == 0 and page_min > -9.5:
            break

    n_zt, n_dt = len(zt), len(dt)
    n_20 = sum(1 for x in zt if x["code"].startswith(("300", "301", "688")))
    by_board = {}
    for board in BOARD_HY_KEYS:
        names = [x for x in zt if hy_match_board(x["hy"], board)]
        hi = max((x["chg"] for x in names), default=0)
        by_board[board] = (len(names), hi)

    if n_zt >= 80 and n_20 >= 6:
        phase, note = "高潮", "跟风可看，不追高标；8因子分不改"
    elif n_zt < 20 or n_dt >= max(15, n_zt):
        phase, note = "退潮", "游资只看不追，不做首板高潮假设；8因子分不改"
    elif n_zt < 40:
        phase, note = "修复", "情绪一般，只做低位转强；8因子分不改"
    else:
        phase, note = "平衡", "正常游资环境；8因子分不改"
    txt = f"情绪{phase}：涨停{n_zt} 跌停{n_dt} 20cm涨停{n_20}。{note}"
    return {
        "phase": phase, "n_zt": n_zt, "n_dt": n_dt, "n_20": n_20,
        "note": note, "by_board": by_board, "txt": txt,
    }


def lhb_warn_map():
    """昨龙虎榜净卖出警示。只标注，不进 8 因子。"""
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
    """表二加列：换手/竞价量/弱转强/超大单/题材板/龙虎 + 连板/昨ZT/板内/竞价额/换手市值。不改 8 因子分。"""
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

    oversold = (
        (ma20 is not None and px < ma20 and bias <= -5)
        or bias <= -8
        or (dd is not None and dd <= -0.15)
        or (rsi_v is not None and rsi_v <= 38)
        or yld
        or downs >= 4
    )
    if not oversold:
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

    if is_limit_up(s["code"], q["chg"]) or q["chg"] >= 7:
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

    if call == "可试仓" and kind != "游资":
        if ma20_diving:
            call, how = "观察", "均线还在加速下跌，超跌钝化，只盯不抄"
        elif idx_weak:
            call, how = "观察", "大盘偏弱，趋势左侧只盯不抄"
        elif not (rsi_ok or vol_ok or dbl_ok):
            call, how = "观察", "缺RSI拐头/缩量再放量/二探，未确认止跌"

    return {
        "score": score, "kind": kind, "call": call, "how": how,
        "loc": loc, "conf": conf, "bias": bias, "pos": pos, "factor": factor,
        "rsi": rsi_v, "dd": dd, "main": main, "fib": fib_retracement(hist, q),
        "confirm": confirm_txt, "fail": fail,
    }


def name_call(s, q, f, yld):
    """Tape call after screen. MA/volume, VWAP. 今涨停硬不追；昨跌停看骗炮/弱转强，不一刀切。"""
    dt = yday_dt_shape(q, yld)
    extra = ""
    if dt == "trap":
        return "不买", "昨跌停骗炮（冲高回落/高开低走）"
    if dt == "turn":
        extra = "；昨跌停弱转强，仓更小"
    elif dt == "weak":
        extra = "；昨跌停次日偏弱"
    if is_limit_up(s["code"], q["chg"]):
        return "不追", "今涨停，空间见顶"
    vwap = q.get("vwap")
    below = vwap is not None and q["px"] < vwap
    above_ma20 = f["ma20"] and q["px"] > f["ma20"]
    hot_rsi = f["rsi"] is not None and f["rsi"] >= 70
    if below and q["chg"] < 0:
        return "不买", "阴跌破分时均价" + extra
    if above_ma20 and not below and q["chg"] < 3 and not hot_rsi:
        return "可小仓", "均价上、涨幅未过热、均线未坏" + extra
    if above_ma20:
        return "观察", "站上20日线，等回踩均价确认" + extra
    return "不买", "均线偏弱" + extra


SKIP_HOW = ("骗炮", "空间见顶", "涨停，", "结束/观察")


def verdict_trend(s, q, f, yld, st):
    """趋势仓最终买点。不改 name_call。"""
    call, why = name_call(s, q, f, yld)
    if call == "可小仓" and st == "回避":
        return "观察", "主线回避"
    return call, why


def verdict_youzi(s, q, f, yld, yz, st, mood, late):
    """游资仓最终买点。和总判同一套闸，表一看这一列就能下结论。"""
    how = (yz or {}).get("how") or ""
    sc = (yz or {}).get("score") or 0
    if yday_dt_shape(q, yld) == "trap":
        return "不买", "昨跌停骗炮"
    if is_limit_up(s["code"], q["chg"]) or q["chg"] >= 7:
        return "不追", "涨停/过热"
    if mood.get("phase") == "退潮":
        return "观察", "情绪退潮"
    if any(k in how for k in SKIP_HOW):
        return "不买", how
    if sc < 65:
        return "观察", f"7a {sc:.0f}未达标"
    if st == "回避":
        return "观察", "主线回避"
    hs = q.get("turnover")
    vr = q.get("vol_ratio") or 0
    if not ((hs is not None and hs >= 5) or vr >= 1.5):
        return "观察", "换手/量比不够"
    if late:
        return "观察", "尾盘不新开"
    return "可小仓", how


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


def main():
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
        fac = score_row(hist, q) if hist and q else None
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
    try:
        mood = market_mood()
    except Exception:
        mood = {
            "phase": "不明", "n_zt": 0, "n_dt": 0, "n_20": 0,
            "note": "涨停统计暂缺", "by_board": {}, "txt": "情绪：暂缺",
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

    tech_lines = {"光通信", "PCB", "半导体", "算力硬件", "算力液冷", "电子元件", "消费电子"}
    med_lines = {"创新药", "游资医药", "医疗", "中药", "医药"}
    avoid = []
    for x in out_lines:
        if x not in avoid:
            avoid.append(x)
    if any(x in out_lines for x in ("电子/科技", "半导体", "电子元件")):
        for x in sorted(tech_lines):
            if x not in avoid:
                avoid.append(x)

    doable = []
    for x in inn_lines:
        if x in avoid:
            continue
        if x not in doable:
            doable.append(x)

    def line_status_of(s):
        line = line_of_board(s.get("board"))
        if line in tech_lines and any(x in avoid for x in tech_lines.union({"电子/科技", "半导体"})):
            return "回避", line
        if line in avoid:
            return "回避", line
        if line == "中药" and "医药" in doable:
            return "中性", line
        if line in doable:
            return "可做", line
        if line in med_lines and line != "中药" and "医药" in doable:
            return "可做", line
        return "中性", line

    heat_map = flow_heat_map(inn, outf)

    def heat_of(line):
        if line in heat_map:
            return heat_map[line]
        if line in tech_lines or line in ("电子/科技", "算力硬件", "算力液冷"):
            for k in ("电子/科技", "半导体", "电子元件", "印制电路板"):
                if k in heat_map:
                    return heat_map[k]
        if line in med_lines:
            for k in ("医药", "医疗", "医疗研发外包"):
                if k in heat_map:
                    return heat_map[k]
        return {}

    def heat_pts_of(st, line):
        h = heat_of(line)
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
    if avoid:
        buy_reason.append("回避主线：" + "、".join(avoid[:8]))
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

    time_bits = timing_pred(now, shapes, doable, [x for x in avoid if x in ("电子/科技", "半导体", "光通信", "PCB", "有色", "电子元件")])
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
    if avoid:
        line_block.append("回避：" + "、".join(avoid[:8]))
    if doable_hits:
        line_block.append("主线上的筛选票：" + "；".join(f"{n} {c}" for n, c, _, _, _ in doable_hits[:8]))
    else:
        line_block.append("主线上的筛选票：无")

    tmin = now.hour * 60 + now.minute
    late_youzi = 14 * 60 + 30 <= tmin <= 15 * 60
    verdicts = {}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        kind = stock_kind(s, q, hist)
        st, line = line_status_of(s)
        if kind == "游资":
            call, why = verdict_youzi(s, q, f, yld, yz_by_code.get(s["code"]), st, mood, late_youzi)
        else:
            call, why = verdict_trend(s, q, f, yld, st)
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
        if call == "可小仓":
            trend_ok.append((s["name"], q["px"], q["chg"], f["buy"], line, st, why, f.get("entry") or 0))
        elif name_call(s, q, f, yld)[0] == "可小仓" and st == "回避":
            trend_no.append(f"{s['name']} 表一可小仓但主线回避")
    youzi_ok, youzi_no = [], []
    if mood.get("phase") == "退潮":
        youzi_no.append("情绪退潮，7a高分也不开游资新仓")
    for s, q, f, yld, yz in yz_youzi:
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "游资", "", ""))
        if call == "可小仓":
            youzi_ok.append((s["name"], q["px"], q["chg"], yz["score"], line, st, why))
        elif yz["score"] >= 65 and call != "不追":
            youzi_no.append(f"{s['name']} 7a {yz['score']:.0f}分 {why}")
    if late_youzi and youzi_ok:
        youzi_no.append("14:30后到收盘，游资只续不新开（7a分不改，仍列出备选）")
    etf_ok = []
    for s, q, f, yld, yz in etf_yz:
        call, why = verdict_etf(q, yz)
        if call == "可小仓":
            etf_ok.append((s["name"], q["px"], q["chg"], yz["score"], why))
    left_ok = [x[0]["name"] + " " + x[4]["call"] for x in left_ranked if x[4]["call"] == "可试仓"]

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
    buy_bits.extend(x[0] for x in youzi_ok)
    buy_bits.extend(x[0] for x in etf_ok)
    buy_line = "、".join(buy_bits) if buy_bits else "没有。不开新仓"

    def auction_pts(auc_call):
        # 竞价轻量加减：不改买点闸。抢筹最多+6，骗炮最多-6，约占值分一成内。
        return {
            "抢筹强": 6, "承接关注": 3, "正常": 0,
            "砸盘弱": -4, "骗炮警惕": -6, "竞价缺": 0,
        }.get(auc_call or "", 0)

    def worth_pts(call, kind, st, line, tape, auc_call=None):
        hp, htag = heat_pts_of(st, line)
        cp = {"可小仓": 50, "可试仓": 24, "观察": 18}.get(call, 0)
        kp = 0
        if call == "可小仓":
            kp = {"趋势": 8, "游资": 5, "ETF": 2}.get(kind, 0)
        elif call == "可试仓":
            kp = 1
        ap = auction_pts(auc_call)
        return cp + kp + hp + 0.28 * (tape or 0) + ap, htag, ap

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

    def record_worth(code, name, kind, q, call, why, line, st, tape, tape_txt, auc_call=None):
        if not code or code in worth_by_code or not q:
            return
        sc, htag, ap = worth_pts(call, kind, st, line, tape, auc_call)
        worth_by_code[code] = {
            "name": name, "kind": kind, "px": q["px"], "chg": q["chg"],
            "call": call, "why": why, "line": line, "st": st,
            "heat": htag, "tape": tape or 0, "tape_txt": tape_txt,
            "score": sc, "role": role_of(call, st), "code": code,
            "auc": auc_call or "-", "auc_pts": ap,
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
        else:
            tape = (f or {}).get("entry") or 0
            tape_txt = f"买点{tape:.0f}"
        auc_call = ((f or {}).get("auction") or {}).get("call")
        record_worth(s["code"], s["name"], kind, q, call, why, line, st, tape, tape_txt, auc_call)
    for s, q, f, yld, yz in etf_yz:
        call, why, kind, _, _ = verdicts.get(s["code"], ("观察", "", "ETF", "ETF", "-"))
        line = ETF_LINE.get(s["name"], "ETF")
        st, line = line_status_of({"board": line if line != "ETF" else s.get("board")})
        tape = (yz or {}).get("score") or 0
        auc_call = ((f or {}).get("auction") or {}).get("call")
        record_worth(s["code"], s["name"], "ETF", q, call, why, line, st, tape, f"ETF {tape:.0f}", auc_call)
    worth_all = list(worth_by_code.values())
    worth_all.sort(key=lambda x: -x["score"])
    for i, r in enumerate(worth_all, 1):
        r["rank"] = i
    top5 = [x for x in worth_all if x["call"] not in ("不买", "不追")][:5]
    top5_line = "、".join(x["name"] for x in top5) if top5 else "没有"

    def wtxt(code):
        w = worth_by_code.get(code)
        if not w:
            return "-", "-"
        return f"{w['score']:.0f}", str(w.get("rank") or "-")

    lines = []
    lines.append(f"# 自选快照 {now.strftime('%Y-%m-%d %H:%M')} 北京")
    lines.append("")
    lines.append("## 0 今日能不能买（先看这一节）")
    lines.append(f"**可以买：{buy_line}**")
    if pick_name:
        lines.append(f"**最适合买：{pick_name}（{pick_why}）**")
    else:
        lines.append("**最适合买：没有。不开新仓**")
    lines.append(f"**今日最值得买 TOP5：{top5_line}**")
    # 隔夜美股→自选关注（8点预案也用这一块；不改买点闸）
    ovn_picks = (ovn_scan or {}).get("picks") or []
    ovn_bias = (ovn_scan or {}).get("bias") or "-"
    if ovn_picks or (ovn_scan or {}).get("leaders"):
        lines.append(f"### 隔夜美股→今日自选关注（{ovn_bias}；只盯不改买点闸）")
        lead_txt = "、".join(
            f"{t['theme']}({t['score']:+.2f})" for t in ((ovn_scan or {}).get("leaders") or [])[:4]
        ) or "无明显领涨"
        lines.append(f"- 隔夜偏好：**{ovn_bias}** — {(ovn_scan or {}).get('bias_why') or ''}")
        lines.append(f"- 领涨主题：{lead_txt}")
        if (ovn_scan or {}).get("oil_note"):
            lines.append(f"- 商品：{(ovn_scan or {}).get('oil_note')}")
        lines.append("| 序 | 标的 | 类型 | 自选板块 | 映射主题 | 隔夜依据 | 态度 |")
        lines.append("|---|---|---|---|---|---|---|")
        if not ovn_picks:
            lines.append("| - | 暂无映射票 | - | - | - | 领涨主题不在自选 | 只看主题 |")
        else:
            for i, p in enumerate(ovn_picks[:8], 1):
                lines.append(
                    f"| {i} | {p['name']} | {p['kind']} | {p['board']} | {p['theme']} | {p['detail']} | **{p['attitude']}** |"
                )
        avoid = (ovn_scan or {}).get("avoid") or []
        if avoid:
            lines.append(
                "- 隔夜偏弱映射（先回避高开）："
                + "、".join(f"{a['name']}({a['theme']})" for a in avoid[:6])
            )
        lines.append("- 说明：这是隔夜预案，开盘后仍以第0节买点闸+竞价+资金主线为准；隔夜强≠可买。")
    lines.append("| 仓 | 股票 | 价 | 今涨 | 买点 | 主线 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|")
    n0 = 0
    for name, px, chg, sc, line, st, why, ent in trend_ok:
        n0 += 1
        _, htag = heat_pts_of(st, line)
        lines.append(f"| 趋势 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {why} |")
    for name, px, chg, sc, line, st, how in youzi_ok:
        n0 += 1
        _, htag = heat_pts_of(st, line)
        lines.append(f"| 游资 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {how} |")
    for name, px, chg, sc, how in etf_ok:
        n0 += 1
        ln = ETF_LINE.get(name, "ETF")
        st, line = line_status_of({"board": ln if ln != "ETF" else ""})
        _, htag = heat_pts_of(st, line)
        lines.append(f"| ETF | {name} | {px:.3f} | {chg:+.2f}% | **可小仓** | {htag} | {how} |")
    for txt in left_ok[:4]:
        n0 += 1
        lines.append(f"| 左侧 | {txt.split()[0]} | - | - | **可试仓** | 超跌 | 轻仓，不替代右侧 |")
    if not n0:
        lines.append("| - | 没有 | - | - | **不买** | - | 不开新仓 |")
    lines.append("### 今日最值得买 TOP5（按值分从高到低，不是表一分；含竞价轻量加减）")
    lines.append("| 序 | 股票 | 仓 | 价 | 今涨 | 买点 | 竞价 | 主线热度 | 盘面 | 值分 | 角色 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    if not top5:
        lines.append("| - | 没有 | - | - | - | - | - | - | - | - | - | 不开新仓 |")
    else:
        for i, r in enumerate(top5, 1):
            px = f"{r['px']:.3f}" if r["kind"] == "ETF" else f"{r['px']:.2f}"
            ap = r.get("auc_pts") or 0
            ap_txt = f"{ap:+.0f}" if ap else "0"
            lines.append(
                f"| {i} | {r['name']} | {r['kind']} | {px} | {r['chg']:+.2f}% | **{r['call']}** | {r.get('auc') or '-'}({ap_txt}) | {r['heat']} | {r['tape_txt']} | **{r['score']:.0f}** | {r['role']} | {r['why']} |"
            )
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
    lines.append("- 可以买=总闸过了才能开仓。TOP5按值分排（买点闸+板块热度+盘面+竞价轻量），不是表一均线分；过闸不够5只用观察票补，备选先盯不买。")
    lines.append("- 竞价进值分但不进买点闸：抢筹强+6、承接+3、正常0、砸盘-4、骗炮-6。骗炮不会单独把可小仓改成不买。")
    lines.append("- 表一看「买点」列：可小仓=能买，观察=盯着，不买/不追=不能买。分只是均线健康。")
    lines.append("")
    ovn_blk = MACRO.get("overnight_external") or {}
    lines.append("## 1 夜间外部市场调研（政策+方向）")
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
    lines.append("## 1b 隔夜美股/金属原油排查→自选次日推荐")
    lines.append(
        "框架：美股指数 + 行业ETF领涨/领跌 + 黄金/白银/铜/原油 + 光通信美股(LITE/COHR)。"
        "领涨主题映射到自选 board，生成次日关注名单。只作预案，不进买点闸、不进表一分/7a。"
    )
    sc = ovn_scan or {}
    lines.append(f"- 隔夜偏好：**{sc.get('bias') or '-'}** — {sc.get('bias_why') or ''}")
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
    lines.append("| 序 | 领涨主题 | 得分 | 隔夜依据 | 映射自选板块 |")
    lines.append("|---|---|---|---|---|")
    leads = sc.get("leaders") or []
    if not leads:
        lines.append("| - | 暂无明显领涨 | - | - | - |")
    else:
        for i, t in enumerate(leads, 1):
            lines.append(
                f"| {i} | **{t['theme']}** | {t['score']:+.2f} | {t['detail']} | {'/'.join(t['boards'])} |"
            )
    lines.append("| 序 | 推荐自选 | 类型 | 板块 | 主题 | 态度 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|")
    picks = sc.get("picks") or []
    if not picks:
        lines.append("| - | 领涨主题不在自选池 | - | - | - | 无 | 只记录主题，不加戏 |")
    else:
        for i, p in enumerate(picks, 1):
            lines.append(
                f"| {i} | {p['name']} | {p['kind']} | {p['board']} | {p['theme']} | **{p['attitude']}** | {p['detail']} |"
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
    lines.append("## 2 国内政策与方向调研")
    for x in dom.get("policy") or []:
        lines.append("- 政策：" + x)
    for x in dom.get("direction") or []:
        lines.append("- 方向：" + x)
    if dom.get("implication"):
        lines.append("- 对今天：" + dom["implication"])
    if not (dom.get("policy") or dom.get("direction")):
        lines.append("- 国内政策调研暂缺")
    lines.append("")
    lines.append("## 3 大盘趋势")
    if idx_lines:
        lines.extend("- " + x for x in idx_lines)
    else:
        lines.append("- 指数暂缺")
    lines.append("")
    lines.append("## 4 板块资金/热度")
    if inn:
        lines.append("流入：" + "；".join(inn[:5]))
    else:
        lines.append("流入：东财主力接口未取到，改看ETF涨跌")
    if outf:
        lines.append("流出：" + "；".join(outf[:4]))
    if etf_lines:
        lines.append("自选ETF：" + "；".join(etf_lines))
    etf_heat = [x for x in ext_lines if any(k in x for k in ("ETF", "医药", "半导体", "通信", "医疗"))]
    if etf_heat:
        lines.append("相关ETF/指数：" + "；".join(etf_heat))
    lines.append("")
    lines.append("东财暗盘资金是模型估算，不是交易所盘前成交。下面用同一套可复现代理：主力/超大单净流入、涨幅与大单是否同向、竞价是否骗炮、板块主力往哪走。")
    lines.append("")
    lines.append("## 4b 自选集合竞价（全池；不进表一分/不进8因子）")
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
    lines.append("- 竞价只作环境层+值分轻量加减：不改买点闸、不改表一分、不改7a。抢筹强≠可买，还要过第0节总闸。值分加减：抢筹+6 / 承接+3 / 正常0 / 砸盘-4 / 骗炮-6。")
    lines.append("")
    def t1_ma_flag(q, f):
        if not f:
            return "-"
        if q["px"] > (f["ma20"] or 0) and q["px"] > (f["ma5"] or 0):
            return "多"
        if q["px"] > (f["ma20"] or 0):
            return "上20"
        return "弱"

    def emit_t1_rows(block, empty_name):
        lines.append("| 序 | 股票 | 买点 | 类型 | 现价 | 今涨 | 分 | 买点分 | 值分 | 均线 | 量比 | 距20日高 | 斜率 | 回踩 | 相对强度 | 板块RS | 量价 | ORB | 昨高 | 布林 | 换手分位 | 仓位 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        if not block:
            lines.append(f"| - | {empty_name} | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - |")
            return
        n = 0
        for s, q, f, yld, hist, buy, kind, call in block:
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
            ws, _ = wtxt(s["code"])
            lines.append(
                f"| {n} | {s['name']} | **{call}** | {kind} | {q['px']:.2f} | {q['chg']:+.2f}% | {sc} | {ent} | {ws} | {t1_ma_flag(q, f)} | {vr} | {dd} | {sl} | {pb} | {rs} | {brs} | {vp} | {orb} | {yhl} | {boll} | {hsp} | {pos} |"
            )

    lines.append(f"## 5 表一（全池{len(t1_all)}只，先按买点再按均线分；趋势{n_trend}+游资{n_youzi}）")
    lines.append("先看「表一买点」。买点=能不能买。分=均线健康。买点分=九列。值分=买点闸+板块热度+盘面+竞价轻量（±6内）。竞价列只展示判断，不单独改买点。")
    lines.append("### 表一买点（先看这张）")
    lines.append("| 序 | 股票 | 买点 | 竞价 | 类型 | 现价 | 今涨 | 分 | 买点分 | 值分 | 值序 | 为什么 |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    if not t1_all:
        lines.append("| - | 表一暂无报价 | - | - | - | - | - | - | - | - | - | - |")
    else:
        n = 0
        for s, q, f, yld, hist, buy, kind, call in t1_all:
            n += 1
            sc = f"{f['buy']:.0f}" if f else "-"
            ent = f"{f['entry']:.0f}" if f and f.get("entry") is not None else "-"
            why = (verdicts.get(s["code"]) or ("", ""))[1] or "-"
            ws, wr = wtxt(s["code"])
            ac = ((f or {}).get("auction") or {}).get("call") or "-"
            lines.append(
                f"| {n} | {s['name']} | **{call}** | {ac} | {kind} | {q['px']:.2f} | {q['chg']:+.2f}% | {sc} | {ent} | **{ws}** | {wr} | {why} |"
            )
    lines.append("")
    lines.append("### 表一明细（均线+九列，只对照，不改买点）")
    emit_t1_rows(t1_all, "表一暂无报价")
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
    lines.append("## 6 表一后再判断（趋势池前8只可操作票，游资不进；原条件未改）")
    if pick_lines:
        lines.extend("- " + x for x in pick_lines)
    else:
        lines.append("- 无合格筛选结果")
    lines.append("")
    lines.append("## 7 表二：游资打分（8因子；游资仓只看7a，表一均线分只对照）")
    lines.append("因子：板块资金｜主力5日｜资金-涨幅同向｜游资弹性（小市值+量比+振幅）｜涨停空间｜竞价质量｜低位启动｜追高罚。昨单日主力接口不稳，用今主力+5日主力代理。")
    lines.append(mood.get("txt") or "情绪：暂缺")
    lines.append("换手/竞价量/弱转强/超大单/题材板/昨龙虎/连板/昨ZT溢价/板内排名/竞价额/换手市值是加列和环境，不进 8 因子分、不改怎么做。")

    def emit_yz_table(title, block):
        lines.append(title)
        lines.append("| 序 | 股票 | 类型 | 板块 | 8因子 | 资金/暗盘代理 | 弹性 | 空间 | 操作分 | 怎么做 | 换手 | 竞价量 | 弱转强 | 超大单 | 题材板 | 昨龙虎 | 连板 | 昨ZT溢价 | 板内排名 | 竞价额 | 换手市值 |")
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
    lines.append("")
    lines.append("## 8 表三：左侧超跌（趋势/游资分开，不改表一表二）")
    lines.append("进池/左侧分仍是原公式：乖离/RSI/距前高超跌 + 收阳或长下影止跌 + 放量承接。斐波那契只标注。趋势可试仓额外要求：均线走平、且 RSI拐头或缩量再放量或二探不破；大盘偏弱则趋势票只观察。游资左侧不要求均线走平。失败：破今日低或 ATR。")

    def emit_left(title, block):
        lines.append(title)
        lines.append("| 序 | 股票 | 类型 | 位置 | 斐波那契 | 止跌确认 | 左侧确认 | 主线 | 左侧分 | 判断 | 怎么做 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        if not block:
            lines.append("| - | 本池暂无超跌 | - | - | - | - | - | - | - | - | - |")
            return
        n = 0
        for s, q, f, yld, ls in block:
            n += 1
            lines.append(
                f"| {n} | {s['name']} | {ls['kind']} | {ls['pos']} | {ls.get('fib') or '-'} | {ls['factor']} | {ls.get('confirm') or '-'} | {ls['line']}/{ls['line_st']} | **{ls['score']:.0f}** | **{ls['call']}** | {ls['how']} |"
            )

    emit_left(f"### 8a 趋势左侧（{len(left_trend)}只）", left_trend)
    emit_left(f"### 8b 游资左侧（{len(left_youzi)}只）", left_youzi)
    lines.append("")
    lines.append("## 9 暗盘口径（7a前10，游资代理，不是点名）")
    for s, q, f, yld, yz in yz_youzi[:10]:
        lines.append(f"- {s['name']}：{yz['flow_txt']}；{yz['sec_txt']}；{yz['auc_txt']}；{yz['factor_line']}")
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
    lines.append("")
    lines.append("## 11 叠加：哪条主线可以做")
    lines.extend("- " + x for x in line_block)
    lines.append("")
    lines.append("## 12 叠加：对照表一个股与主线")
    if overlay_lines:
        lines.extend("- " + x for x in overlay_lines)
    else:
        lines.append("- 无")
    lines.append("")
    lines.append("## 13 叠加：时点预测")
    lines.extend("- " + x for x in time_bits)
    lines.append("")
    lines.append("## 14 综合：今日能不能买（明细，结论同第0节）")
    lines.append(f"**综合结论：{buy_today}**")
    if pick_name:
        lines.append(f"**最适合买：{pick_name}（{pick_why}）**")
    else:
        lines.append("**最适合买：没有。不开新仓**")
    lines.append(f"**今日可以买：{buy_line}**")
    lines.append(f"**今日最值得买 TOP5：{top5_line}（含备选，表同第0节）**")
    lines.append("| 仓 | 股票 | 价 | 今涨 | 分 | 主线 | 依据 | 适合度 | 结论 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
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
        lines.append(f"| 趋势 | {name} | {px:.2f} | {chg:+.2f}% | 表一{sc:.0f}/买点{ent:.0f} | {line}/{st} | {why} | {fit} | **可小仓** |")
    for name, px, chg, sc, line, st, how in youzi_ok:
        n_buy_rows += 1
        fit = "尾盘不新开" if late_youzi else mark_row(name, youzi_best)
        lines.append(f"| 游资 | {name} | {px:.2f} | {chg:+.2f}% | 7a {sc:.0f} | {line}/{st} | {how} | {fit} | **可小仓** |")
    for name, px, chg, sc, how in etf_ok:
        n_buy_rows += 1
        fit = "首选" if pick_name == name else "可买"
        lines.append(f"| ETF | {name} | {px:.3f} | {chg:+.2f}% | ETF分 {sc:.0f} | ETF | {how} | {fit} | **可小仓** |")
    for txt in left_ok[:6]:
        n_buy_rows += 1
        nm = txt.split()[0]
        fit = "首选" if pick_name == nm else "轻仓备选"
        lines.append(f"| 左侧 | {txt} | - | - | 表三 | 超跌 | 轻仓试，不替代右侧 | {fit} | **可试仓** |")
    if not n_buy_rows:
        lines.append("| - | 没有同时满足分层条件的票 | - | - | - | - | 表一可小仓+主线未回避；或7a≥65且未追高/未回避；或表三可试仓 | - | **不买** |")
    lines.append("")
    lines.append("总判规则：趋势=表一可小仓且主线不是回避（同主线按买点分优先）；游资=7a≥65、未涨停/未骗炮、主线不是回避、情绪非退潮、换手≥5%或量比≥1.5；ETF=表一ETF对照、8因子可小仓且分≥60涨幅<5%（不单独列表）；左侧=表三可试仓（轻仓）。最适合买按层排序：趋势可小仓 > 游资达标 > ETF > 左侧。表一分不混买点分，8因子不混表一分。昨跌停看骗炮/弱转强，昨涨停不剔除，今涨停不追。")
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
    lines.append("- 趋势仓：只看表一趋势池可小仓；游资不进表一买点，也不把8因子/回踩/斜率加进均线分")
    if mood.get("phase") == "退潮":
        lines.append("- 游资仓：情绪退潮，表二 8 因子高分也只看不追（分本身不改）")
    elif mood.get("phase") == "高潮":
        lines.append("- 游资仓：情绪高潮，可看跟风，仍不追高标；8因子分不改")
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
        "time": now.isoformat(),
        "buy_today": buy_today,
        "doable": doable,
        "avoid": avoid[:10],
        "can_small": can_small,
        "watch": watch,
        "small_on_line": small_on_line,
        "buy_names": buy_bits,
        "top5": [{"name": x["name"], "role": x["role"], "score": round(x["score"], 1), "heat": x["heat"]} for x in top5],
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
    keys = ("状态", "判断", "怎么做", "操作分", "买点", "角色", "竞价判断", "竞价")
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
:root { --bg:#f6f7fb; --card:#fff; --line:#d8dee9; --ink:#1c2430; --muted:#5c6b7a; }
* { box-sizing:border-box; }
body { margin:0; font-family:-apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;
  background:var(--bg); color:var(--ink); line-height:1.45; }
nav { position:sticky; top:0; z-index:30; background:#10243f; color:#fff; padding:10px 12px;
  display:flex; gap:10px; overflow-x:auto; white-space:nowrap; font-size:14px; }
nav a { color:#d7e7ff; text-decoration:none; padding:4px 8px; border-radius:6px; }
nav a:hover { background:#1e3a5f; }
.page { padding:12px 14px 48px; max-width:100%; }
h1 { font-size:22px; margin:8px 0 12px; }
h2 { font-size:18px; margin:28px 0 10px; padding-top:8px; border-top:1px solid var(--line); }
h2#s0 { background:#0d5c2e; color:#fff; border:none; padding:12px 14px; border-radius:8px 8px 0 0; margin:8px 0 0; }
h2#s0 + p { background:#e8f6ee; margin:0; padding:10px 14px 4px; font-size:17px; font-weight:700; color:#0d5c2e; }
h2#s0 + p + p { background:#e8f6ee; margin:0; padding:4px 14px 4px; font-size:15px; font-weight:700; }
h2#s0 + p + p + p { background:#e8f6ee; margin:0; padding:4px 14px 10px; font-size:15px; font-weight:700; }
h3 { font-size:16px; margin:18px 0 8px; color:#243447; }
p, li { font-size:14px; }
ul { padding-left:1.2em; }
.note { color:var(--muted); font-size:13px; margin:6px 0; }
.wrap { overflow-x:auto; -webkit-overflow-scrolling:touch; border:1px solid var(--line);
  border-radius:8px; background:var(--card); margin:8px 0 16px; }
table { border-collapse:collapse; min-width:980px; width:max-content; font-size:13px; }
th, td { border-bottom:1px solid #edf1f6; padding:6px 8px; text-align:left; white-space:nowrap; }
td.wrapcell, th.wrapcell { white-space:normal; min-width:140px; max-width:280px; }
th { background:#16324f; color:#fff; position:sticky; top:0; z-index:2; font-weight:600; }
th:nth-child(2), td:nth-child(2) { position:sticky; left:0; z-index:1; background:#fff;
  box-shadow:2px 0 0 #edf1f6; font-weight:600; }
th:nth-child(2) { z-index:3; background:#16324f; }
tr:nth-child(even) td { background:#fafbfd; }
tr:nth-child(even) td:nth-child(2) { background:#fafbfd; }
.up { color:#d0021b; font-weight:600; }
.dn { color:#0a8f3d; font-weight:600; }
.ok { color:#0a8f3d; font-weight:700; }
.bad { color:#d0021b; font-weight:700; }
.watch { color:#b36b00; font-weight:700; }
.hint { position:sticky; bottom:0; background:#fff8e6; border-top:1px solid #f0d48a;
  padding:8px 12px; font-size:12px; color:#6a5416; z-index:20; }
@media print {
  @page { size: A3 landscape; margin: 8mm; }
  nav, .hint { display:none; }
  .wrap { overflow:visible; border:none; }
  table { font-size:10px; min-width:0; width:100%; }
  th, td { white-space:normal; }
  th { position:static; }
  th:nth-child(2), td:nth-child(2) { position:static; box-shadow:none; }
}
"""
    parts = [
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="X-UA-Compatible" content="IE=edge">',
        f"<title>{html.escape(title)}</title><style>{css}</style></head><body>",
        "<nav>",
        "<a href='#s0'>0能不能买</a>",
        "<a href='#s1'>1外盘</a><a href='#s1b'>1b隔夜推荐</a><a href='#s2'>2国内</a><a href='#s3'>3大盘</a>",
        "<a href='#s4'>4资金</a><a href='#s4b'>4b竞价</a><a href='#s5'>5表一</a><a href='#s6'>6再判断</a>",
        "<a href='#s7'>7表二</a><a href='#s8'>8表三</a><a href='#s14'>14明细</a>",
        "</nav><div class=page>",
    ]
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.rstrip()
        if not line.strip():
            i += 1
            continue
        if line.startswith("# "):
            parts.append(f"<h1>{inline_md(line[2:])}</h1>")
            i += 1
            continue
        if line.startswith("## "):
            title_txt = line[3:].strip()
            m = re.match(r"^(\d+[a-z]?)", title_txt)
            hid = f"s{m.group(1)}" if m else ""
            parts.append(f"<h2 id='{hid}'>{inline_md(title_txt)}</h2>")
            i += 1
            continue
        if line.startswith("### "):
            parts.append(f"<h3>{inline_md(line[4:])}</h3>")
            i += 1
            continue
        if line.startswith("|"):
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
            wrap_headers = {"8因子", "资金/暗盘代理", "怎么做", "止跌确认", "左侧确认", "仓位", "为什么", "依据", "主线热度", "主线"}
            parts.append("<div class=wrap><table><thead><tr>")
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
            parts.append("</tbody></table></div>")
            continue
        if line.startswith("- "):
            parts.append("<ul>")
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                parts.append(f"<li>{inline_md(lines[i].lstrip()[2:])}</li>")
                i += 1
            parts.append("</ul>")
            continue
        parts.append(f"<p class=note>{inline_md(line)}</p>")
        i += 1
    parts.append("</div>")
    parts.append("<div class=hint>横滑看完整表格；左侧股票名冻结。这是展示层，打分/买点与 markdown 同一份数据。</div>")
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
    body = render_report_html(md, f"自选快照 {clock} 北京")
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

