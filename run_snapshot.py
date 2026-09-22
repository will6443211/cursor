#!/usr/bin/env python3
"""Hourly watchlist: external + index + sector flow + MA/volume screen + buy/no-buy."""
import json, urllib.request, urllib.parse, hashlib, datetime, os, sys, html, re, subprocess, webbrowser, difflib
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
FLOW_STALE = []


def _note_fail(tag):
    FETCH_FAIL[tag] = FETCH_FAIL.get(tag, 0) + 1


def _note_stale(tag):
    if tag not in FLOW_STALE:
        FLOW_STALE.append(tag)


def _em_diff(d):
    """东财 clist/ulist 的 diff 有时是 list，有时是 {0:row,1:row}。当成 key 迭代会整表吃空。"""
    data = (d or {}).get("data") or {}
    diff = data.get("diff")
    if isinstance(diff, dict):
        rows = list(diff.values())
    elif isinstance(diff, list):
        rows = diff
    else:
        rows = []
    return [x for x in rows if isinstance(x, dict)]


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


def http(url, gbk=False, timeout=12, tries=4):
    headers = {
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
    }
    last = None
    for i in range(tries):
        try:
            b = _get(url, headers, timeout, 1)
            if gbk:
                return b.decode("gbk", "replace")
            if not (b or b"").strip():
                raise ValueError("empty body")
            return json.loads(b)
        except Exception as e:
            last = e
            if i < tries - 1:
                import time as _t
                _t.sleep(0.5 * (2 ** i))
    raise last


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


def cn_session_closed(now=None):
    """周末或非连续竞价时段：按收盘后，不按墙上时钟的 9:30-15:00。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.weekday() >= 5:
        return True
    hm = now.strftime("%H:%M")
    return hm < "09:30" or hm >= "15:00"


def cn_session_live(now=None):
    """A股连续竞价（含集合竞价尾声）。报价/涨停统计从 09:15 起用实时。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return "09:15" <= hm < "15:05"


def cn_flow_live(now=None):
    """主力净流入从 9:30 连续成交才有新信息。竞价/休市仍用昨收，不是盘中 delay。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.weekday() >= 5:
        return False
    hm = now.strftime("%H:%M")
    return "09:30" <= hm < "15:05"


def em_flow_hosts(path, now=None):
    """资金接口。开盘只用实时（push2 + 备用节点）；休市 live 常关，delay 就是昨收最新。"""
    live = f"https://push2.eastmoney.com{path}"
    live2 = f"https://82.push2.eastmoney.com{path}"
    delay = f"https://push2delay.eastmoney.com{path}"
    if cn_flow_live(now):
        return (live, live2)
    return (live, live2, delay)


def youzi_late(now=None):
    """游资/打板不新开：周末、盘前、14:30后、收盘后。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.weekday() >= 5:
        return True
    tmin = now.hour * 60 + now.minute
    return tmin < 9 * 60 + 30 or tmin >= 14 * 60 + 30


MEAL_PATH = os.path.join(ROOT, "reports", "overnight_meal.json")
LINE_HIST_PATH = os.path.join(ROOT, "reports", "line_hist.json")
MEAL_ZT_DAYS = 10
MEAL_LINE_DAYS = 3


def overnight_meal_phase(now=None):
    """尾盘隔夜仓自己的钟。不复用 youzi_late：14:30 后游资空仓，尾盘隔夜仓才开始。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    if now.weekday() >= 5:
        return "off"
    hm = now.strftime("%H:%M")
    if "09:15" <= hm < "10:00":
        return "sell"
    if "14:30" <= hm < "14:57":
        return "buy"
    if "14:57" <= hm < "15:00":
        return "too_late"
    if "10:00" <= hm < "14:30":
        return "wait"
    return "off"


def overnight_meal_load():
    try:
        return json.load(open(MEAL_PATH, encoding="utf-8")) or {}
    except Exception:
        return {}


def overnight_meal_save(date_s, picks, now):
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    blob = {
        "date": date_s,
        "saved": now.isoformat(),
        "picks": picks,
    }
    open(MEAL_PATH, "w", encoding="utf-8").write(json.dumps(blob, ensure_ascii=False, indent=2))


def meal_hot_lines(date_s, today_doable):
    """近3个交易日资金主线（可做）。写入当天，缺历史只用今天；文件空不把全市场否决。"""
    days = {}
    try:
        blob = json.load(open(LINE_HIST_PATH, encoding="utf-8")) or {}
        raw = blob.get("days") if isinstance(blob, dict) else None
        days = raw if isinstance(raw, dict) else {}
    except Exception:
        days = {}
    today = [x for x in (today_doable or []) if x]
    if date_s and today:
        days[date_s] = today
        keep = sorted(k for k in days if re.match(r"^\d{4}-\d{2}-\d{2}$", str(k)))[-15:]
        days = {k: days[k] for k in keep}
        try:
            os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
            open(LINE_HIST_PATH, "w", encoding="utf-8").write(
                json.dumps({"days": days}, ensure_ascii=False, indent=2)
            )
        except Exception:
            pass
    hot = set(today)
    if date_s:
        try:
            d = datetime.datetime.strptime(str(date_s)[:10], "%Y-%m-%d").date()
        except Exception:
            d = None
        got, guard = 0, 0
        while d is not None and got < MEAL_LINE_DAYS and guard < 20:
            if d.weekday() < 5:
                for x in days.get(d.strftime("%Y-%m-%d")) or []:
                    hot.add(x)
                got += 1
            d -= datetime.timedelta(days=1)
            guard += 1
    return hot


def meal_line_ok(line, st, hot):
    """近3日主线才做隔夜。今天可做一定算；中性票要在近3日资金主线里。主线数据全空则放过，避免取数失败清空。"""
    if st == "可做":
        return True
    if not hot:
        return True
    if line and line in hot:
        return True
    if line in MED_LINES and (set(hot) & MED_LINES or "医药" in hot):
        return True
    return False


def display_kind(kind):
    """报告/首页给人看的仓名。内部 journal kind 仍用 打板。"""
    k = (kind or "").strip()
    return {
        "打板": "早盘接力仓",
        "尾盘打板": "尾盘狙击",
        "尾盘隔夜仓": "尾盘狙击",
        "尾盘隔夜狙击": "尾盘狙击",
        "午夜关注": "尾盘狙击",
        "隔夜饭": "尾盘狙击",
    }.get(k, k or "-")


def style_books():
    """四仓买卖钟。按A股短线常见高胜率做法，不是席位T+0。尾盘隔夜仓=隔夜套利，单独一仓。"""
    return [
        {
            "kind": "游资",
            "buy": "09:35–10:15 主买（过闸+均价上+主线在）；10:15–10:30 回踩只补一次",
            "skip": "09:30–09:35 开盘三分钟、10:30后、14:00后尾盘",
            "sell": "次日 09:31–09:50 冲高卖，最晚 10:00；低开 09:30–09:40 先走",
            "hold": "只隔一夜，不隔第二夜。昨仓今天下午 14:00 前还能出，那是出旧仓",
        },
        {
            "kind": "趋势",
            "buy": "10:00–10:30 回踩均价/均线；13:00–13:30 资金回流再确认",
            "skip": "开盘抢筹、14:00后追新高",
            "sell": "止盈：之后某日 09:45–10:15 冲高减，不要第一秒砸。止损：跌破计划位马上走。主线走弱：次日早盘清",
            "hold": "按均线/主线拿几天，不是当天出完",
        },
        {
            "kind": "早盘接力仓",
            "buy": "昨首板今接力 09:32–09:50 不炸再打；弱转强 09:35–10:00 低开翻红站住均价",
            "skip": "今首板、尾盘偷鸡、一字板、二进三硬打",
            "sell": "次日不封/开板 09:30–09:45 走；冲高不封 09:35–10:00 卖；续板一字可留",
            "hold": "未晋级不隔第二夜。封死才看到下一板",
        },
        {
            "kind": "尾盘狙击",
            "buy": "14:40–14:55 主买（第1节可尾盘：隔夜强势或尾盘二板）。14:30–14:40只看盘，14:57不追。独立仓，不进今日必买",
            "skip": "14:00–14:30假拉、尾盘首板偷鸡、14:57收盘集合、均价下、近10日无涨停、非近3日主线、未过第1节闸",
            "sell": "次日 09:31–09:50 冲高卖（约+3%走）；低开/开板 09:30–09:35 先走（约-2%）；最晚 10:00 清完。竞价可挂",
            "hold": "只隔一夜。一字涨停可暂留，其余不隔第二夜。独立于游资/趋势/打板，不进第0节可以买",
        },
    ]


def style_now_action(now=None):
    """这一分钟：四仓是该买、该卖旧仓，还是空仓。尾盘隔夜仓买卖窗跟游资反着。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hm = now.strftime("%H:%M")
    off = {
        "youzi_buy": "不买", "youzi_sell": "不卖新仓",
        "trend_buy": "不买", "trend_sell": "不赶着卖",
        "daban_buy": "不买", "daban_sell": "不卖新仓",
        "meal_buy": "不买", "meal_sell": "不卖新仓",
    }
    if now.weekday() >= 5 or hm < "09:15" or hm >= "15:00":
        return {
            **off,
            "youzi_sell": "等下一开盘", "trend_sell": "等下一开盘",
            "daban_sell": "等下一开盘",
            "meal_buy": "等下一尾盘", "meal_sell": "等下一开盘",
        }
    if "09:15" <= hm < "09:30":
        return {
            "youzi_buy": "不买，看竞价", "youzi_sell": "昨仓可挂卖",
            "trend_buy": "不买", "trend_sell": "昨仓可挂，高开不必砸",
            "daban_buy": "看封单，不下手", "daban_sell": "昨板今低开/开板：竞价先挂",
            "meal_buy": "不买", "meal_sell": "昨仓竞价可挂（一字涨停留）",
        }
    if "09:30" <= hm < "09:35":
        return {
            "youzi_buy": "等3分钟", "youzi_sell": "昨仓低开先走，高开再等",
            "trend_buy": "等3分钟", "trend_sell": "低开砸到止损才走，高开别第一秒卖",
            "daban_buy": "等3分钟看炸板", "daban_sell": "开板立刻走；高开封死先留",
            "meal_buy": "不买", "meal_sell": "低开/开板先走；高开等冲",
        }
    if "09:35" <= hm < "10:00":
        return {
            "youzi_buy": "主买点", "youzi_sell": "昨仓主卖点（冲高走）",
            "trend_buy": "确认均价后可买", "trend_sell": "昨仓冲高可减一截",
            "daban_buy": "一进二/弱转强主买点", "daban_sell": "不封就卖，封死再留",
            "meal_buy": "不买", "meal_sell": "主卖点（冲高约+3%走，最晚10:00）",
        }
    if "10:00" <= hm < "10:30":
        return {
            "youzi_buy": "回踩补一次", "youzi_sell": "昨仓最晚清完",
            "trend_buy": "主买点（回踩）", "trend_sell": "没到止盈止损就拿着",
            "daban_buy": "未封死不追", "daban_sell": "还没走的昨板，这会儿清",
            "meal_buy": "等到14:30", "meal_sell": "已过卖窗，残仓按止损清",
        }
    if "10:30" <= hm < "11:30":
        return {
            "youzi_buy": "原则上不新开", "youzi_sell": "旧仓能出就出",
            "trend_buy": "回踩均线仍可买", "trend_sell": "破位才走",
            "daban_buy": "高位不追", "daban_sell": "未晋级的应已走掉",
            "meal_buy": "等到14:30", "meal_sell": "已过卖窗",
        }
    if "11:30" <= hm < "13:00":
        return {**off, "youzi_buy": "停", "trend_buy": "停", "daban_buy": "停", "meal_buy": "停"}
    if "13:00" <= hm < "13:30":
        return {
            "youzi_buy": "资金回流才跟，否则空", "youzi_sell": "旧仓下午弱就出",
            "trend_buy": "第二买点（回流确认）", "trend_sell": "主线还在就拿",
            "daban_buy": "回流才看，不追高", "daban_sell": "开板的不要拖到尾盘",
            "meal_buy": "等到14:30", "meal_sell": "已过卖窗",
        }
    if "13:30" <= hm < "14:00":
        return {
            "youzi_buy": "不新开", "youzi_sell": "旧仓弱则出",
            "trend_buy": "过闸仍可买", "trend_sell": "主线还在就拿",
            "daban_buy": "不新开", "daban_sell": "残仓清掉",
            "meal_buy": "等到14:30", "meal_sell": "已过卖窗",
        }
    if "14:00" <= hm < "14:30":
        return {
            "youzi_buy": "原则上不新开（胜率差）", "youzi_sell": "旧仓 14:00 前尽量出完",
            "trend_buy": "不追新高，回踩才看", "trend_sell": "主线净出则准备次日清",
            "daban_buy": "不新开，改看尾盘隔夜仓", "daban_sell": "残仓清掉",
            "meal_buy": "等到14:30看盘，现在是假拉高发", "meal_sell": "今天还没买",
        }
    if "14:30" <= hm < "14:40":
        return {
            "youzi_buy": "不新开", "youzi_sell": "今天新买的不能卖",
            "trend_buy": "不追新高", "trend_sell": "不因尾盘情绪乱出",
            "daban_buy": "不新开", "daban_sell": "今天新买的不能卖",
            "meal_buy": "看盘确认，不抢14:30假拉", "meal_sell": "今天新买的不能卖",
        }
    if "14:40" <= hm < "14:57":
        return {
            "youzi_buy": "不新开", "youzi_sell": "今天新买的不能卖",
            "trend_buy": "不追新高", "trend_sell": "不因尾盘情绪乱出",
            "daban_buy": "不新开", "daban_sell": "今天新买的不能卖",
            "meal_buy": "主买点（第1节可尾盘才下）", "meal_sell": "今天新买的不能卖",
        }
    return {
        "youzi_buy": "不新开", "youzi_sell": "今天新买的不能卖",
        "trend_buy": "不追新高", "trend_sell": "不因尾盘情绪乱出",
        "daban_buy": "不新开", "daban_sell": "今天新买的不能卖",
        "meal_buy": "不追收盘集合", "meal_sell": "今天新买的不能卖",
    }


def buy_clock(now=None):
    """A股主流下手钟。09:30–14:30 不是随时买：游资有黄金段，趋势看回踩，尾盘隔夜仓只吃尾盘。
    散户没有游资席位的 T+0，「上午买下午卖」要整体滞后一天。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hm = now.strftime("%H:%M")
    retail = (
        "游资席位「上午买下午卖」是T+0（底仓/对倒），散户A股T+1做不到。"
        "散户映射：今天09:35–10:30买 → 明天09:30–10:00冲高卖（整体滞后一天）。"
        "昨天已有的仓今天下午可以卖，那是卖旧仓，不是当天买当天卖。"
        "尾盘隔夜仓才是散户合法的隔夜套利：尾盘买、次日早盘卖，单独看第1节，不进第0节可以买。"
    )
    table = [
        ("09:15-09:25", "集合竞价", "只看开板/骗炮，不下手。尾盘隔夜仓昨仓可挂卖，一字涨停不卖"),
        ("09:25-09:30", "竞价结果", "确认炸板/骗炮，仍不追"),
        ("09:30-09:35", "开盘三分钟", "等方向，不追飞刀。尾盘隔夜仓低开先走，高开等冲"),
        ("09:35-10:00", "游资黄金买点", "过闸+均价上+主线在→下手。尾盘隔夜仓主卖点（冲高约+3%）"),
        ("10:00-10:30", "回踩确认", "10:30再刷；均价/主线还在就补买。尾盘隔夜仓10:00前必须清完"),
        ("10:30-11:30", "上午后半", "趋势回踩均线/均价；游资弹性变差，过闸仍可跟"),
        ("11:30-13:00", "午休", "不交易"),
        ("13:00-13:30", "午后确认", "趋势第二窗口；游资看资金有没有回流"),
        ("13:30-14:00", "午后中段", "趋势可买；游资谨慎"),
        ("14:00-14:30", "游资最后窗口", "过闸才买，14:30截止。尾盘隔夜仓还等，这段假拉多"),
        ("14:30-14:40", "尾盘观察", "游资空仓。隔夜套利先看封死/均价，不抢14:30假拉"),
        ("14:40-14:55", "尾盘狙击主买", "第1节可尾盘才买；14:40后更稳，14:57不追"),
        ("14:57-15:00", "收盘集合", "不追脉冲"),
    ]
    empty = {
        "slot": "休市", "name": "休市", "action": "不新开",
        "youzi": "不新开", "trend": "不新开", "daban": "不新开", "meal": "不做",
        "retail": retail, "table": table,
    }
    if now.weekday() >= 5:
        return {**empty, "action": "周末不新开；最新资讯只盯，买点等下一交易日09:30过闸"}
    if hm < "09:15" or hm >= "15:00":
        return {
            **empty, "slot": "盘前/收盘后", "name": "休市",
            "action": "不新开。最新资讯看最前一节，买点等次日09:30过闸",
            "trend": "预案，次日开盘再确认",
            "meal": "错过则等下一尾盘",
        }
    if "09:15" <= hm < "09:30":
        return {
            **empty, "slot": "09:15-09:30", "name": "集合竞价",
            "action": "只看开板/骗炮，集合竞价不下手。尾盘隔夜仓昨仓可挂卖",
            "youzi": "看盘", "trend": "看盘", "daban": "看封单",
            "meal": "卖尾盘隔夜仓（竞价可挂）",
        }
    if "09:30" <= hm < "09:35":
        return {
            **empty, "slot": "09:30-09:35", "name": "开盘三分钟",
            "action": "等方向，不追飞刀。尾盘隔夜仓低开先走，高开等冲",
            "youzi": "等3分钟", "trend": "等3分钟", "daban": "等3分钟看炸板",
            "meal": "冲高先卖 / 低开先走",
        }
    if "09:35" <= hm < "10:00":
        return {
            **empty, "slot": "09:35-10:00", "name": "游资黄金买点",
            "action": "过闸+均价上+主线在→下手。尾盘隔夜仓这会儿是主卖点，不是买点",
            "youzi": "主买点", "trend": "可买（确认均价）", "daban": "一进二主买点",
            "meal": "主卖点，10:00前必须卖完",
        }
    if "10:00" <= hm < "10:30":
        return {
            **empty, "slot": "10:00-10:30", "name": "回踩确认",
            "action": "10:30再刷；均价/主线还在就补买，这是第二买点",
            "youzi": "第二买点", "trend": "主买点（回踩）", "daban": "未封死不追",
            "meal": "已过卖窗",
        }
    if "10:30" <= hm < "11:30":
        return {
            **empty, "slot": "10:30-11:30", "name": "上午后半",
            "action": "趋势回踩均线/均价下手；游资若还过闸可跟，弹性已差",
            "youzi": "过闸可跟", "trend": "回踩下手", "daban": "高位不追",
            "meal": "已过卖窗",
        }
    if "11:30" <= hm < "13:00":
        return {
            **empty, "slot": "11:30-13:00", "name": "午休",
            "action": "不交易。下午开盘再看资金主线是否延续",
            "youzi": "停", "trend": "停", "daban": "停", "meal": "停",
        }
    if "13:00" <= hm < "13:30":
        return {
            **empty, "slot": "13:00-13:30", "name": "午后确认",
            "action": "趋势第二窗口；游资看资金有没有回流，回流才跟",
            "youzi": "资金回流才买", "trend": "第二买点", "daban": "回流才看",
            "meal": "已过卖窗",
        }
    if "13:30" <= hm < "14:00":
        return {
            **empty, "slot": "13:30-14:00", "name": "午后中段",
            "action": "趋势可买；游资谨慎，没有回流不接",
            "youzi": "谨慎", "trend": "可买", "daban": "谨慎",
            "meal": "已过卖窗",
        }
    if "14:00" <= hm < "14:30":
        return {
            **empty, "slot": "14:00-14:30", "name": "游资最后窗口",
            "action": "过闸才买，14:30截止。尾盘隔夜仓还等，这段假拉多、胜率差",
            "youzi": "最后买点", "trend": "可买", "daban": "14:30前最后看",
            "meal": "等到14:30看盘",
        }
    if "14:30" <= hm < "14:40":
        return {
            **empty, "slot": "14:30-14:40", "name": "尾盘观察",
            "action": "游资/早盘接力不新开。尾盘隔夜仓先看封死、均价、资金还在，不抢14:30假拉",
            "youzi": "不新开", "trend": "可买但不追新高", "daban": "不新开，改看第1节",
            "meal": "看盘确认，14:40后再下",
        }
    if "14:40" <= hm < "14:57":
        return {
            **empty, "slot": "14:40-14:55", "name": "尾盘狙击主买",
            "action": "游资/早盘接力不新开。尾盘隔夜仓主买点：第1节可尾盘才下，14:57不追",
            "youzi": "不新开", "trend": "可买但不追新高", "daban": "不新开，改看第1节",
            "meal": "真开仓（主买点）",
        }
    return {
        **empty, "slot": "14:57-15:00", "name": "收盘集合",
        "action": "不追尾盘脉冲",
        "youzi": "不新开", "trend": "不追", "daban": "不新开",
        "meal": "14:57后不追",
    }


def session_progress(now=None):
    """(已走时间占比, 应完成成交量占比)。收盘后/盘前/周末都给 (1,1)，阈值不做时段调整。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    hm = now.strftime("%H:%M")
    if cn_session_closed(now):
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
    """昨跌停次日：trap骗炮 / turn弱转强 / weak续弱。
    高开 0.5% 且现价略低于开盘太容易误杀；骗炮要高开明显，并且真走弱（收绿/破均价/冲高回落）。"""
    if not yld or not q or not q.get("prev"):
        return None
    o, p, prev = q["open"], q["px"], q["prev"]
    h = q["high"]
    vwap = q.get("vwap")
    gap = (o / prev - 1) * 100
    chg = q["chg"]
    vr = vr_norm(q.get("vol_ratio") or 0)
    drop_h = ((h - p) / h * 100) if h else 0
    below_vwap = bool(vwap) and p < vwap
    weak_tape = chg < 0 or below_vwap or drop_h >= 1.0
    if gap >= 1.0 and p < o and weak_tape:
        return "trap"
    if gap <= -0.4 and h > prev and p < o and (chg < 0 or below_vwap or drop_h >= 1.2):
        return "trap"
    if chg > 0 and gap <= -0.8 and p >= o and (vwap is None or p >= vwap) and vr >= 1.0:
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


BIG_CAP_YI = 400.0  # 超过这个市值的票不当游资标的，也不做尾盘隔夜仓，不管代码是不是 300/688


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


def session_kind(code, raw):
    """同一交易日不换仓种，避免均线闸和 7a 闸来回跳。"""
    if raw == "ETF":
        return "ETF"
    prev = (GATE_PREV.get(code) or {}).get("kind")
    if prev in ("趋势", "游资") and raw in ("趋势", "游资"):
        return prev
    return raw


def stock_flow_ok(flow):
    """接口里有这条就算有资金数据；主力=0 是真的平，缺字段才是没拉到。"""
    return isinstance(flow, dict) and ("main" in flow or "main5" in flow)


def youzi_enter_need(yz, mood=None):
    """游资可小仓门槛。资金缺或情绪退潮时抬高，不把缺数据当成中性放行。"""
    need = 65
    if (yz or {}).get("flow_miss"):
        need = 72
    if (mood or {}).get("phase") == "退潮":
        need = max(need, 75)
    return need


def em_secid(s):
    return f"{0 if s['market'] == 'sz' else 1}.{s['code']}"


FLOW_CACHE = os.path.join(ROOT, "reports", "flow_cache.json")


def _flow_cache_load(max_hours=18, same_session=False):
    try:
        blob = json.load(open(FLOW_CACHE, encoding="utf-8"))
        ts = blob.get("_saved") or ""
        saved = datetime.datetime.fromisoformat(ts)
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=now.tzinfo)
        if (now - saved).total_seconds() > max_hours * 3600:
            return None
        if same_session and blob.get("date") != session_date(now):
            return None
        blob["_from_cache"] = True
        return blob
    except Exception:
        return None


def _flow_cache_save(inn=None, outf=None, stocks=None):
    try:
        os.makedirs(os.path.dirname(FLOW_CACHE), exist_ok=True)
        prev = {}
        try:
            prev = json.load(open(FLOW_CACHE, encoding="utf-8"))
        except Exception:
            prev = {}
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        blob = {
            "inn": inn if inn else (prev.get("inn") or []),
            "out": outf if outf else (prev.get("out") or []),
            "stocks": dict(prev.get("stocks") or {}),
            "_saved": now.isoformat(),
            "date": session_date(now),
        }
        if stocks:
            blob["stocks"].update(stocks)
        if blob["inn"] or blob["out"] or blob["stocks"]:
            json.dump(blob, open(FLOW_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def stock_flow(stocks):
    """主力/超大单净流入代理。东财暗盘不是真成交，这是可复现口径。
    开盘只用实时；休市先爬 live，连不上再用 delay（昨收），最后才同交易日缓存。
    盘中 delay 不用。实时抖动时用今日已成功缓存，隔日缓存不开盘后冒充当天。"""
    out = {}
    ids = [em_secid(s) for s in stocks]
    fields = "f12,f14,f62,f184,f66,f69,f164,f165"
    hosts = em_flow_hosts("/api/qt/ulist.np/get")

    def eat(diff):
        for x in diff or []:
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

    for host in hosts:
        for i in range(0, len(ids), 18):
            chunk = ",".join(ids[i:i + 18])
            url = f"{host}?fltt=2&invt=2&np=1&fields={fields}&secids={chunk}"
            try:
                d = http(url, timeout=12)
            except Exception:
                continue
            eat(_em_diff(d))
        if len(out) >= max(1, int(len(ids) * 0.5)):
            break
    miss = [s["code"] for s in stocks if s.get("code") not in out]
    if miss:
        cached = (_flow_cache_load(same_session=True) or {}).get("stocks") or {}
        n_fill = 0
        for code in miss:
            if stock_flow_ok(cached.get(code)):
                row = dict(cached[code])
                row["_from_cache"] = True
                out[code] = row
                n_fill += 1
        if n_fill:
            _note_stale("个股资金用今日缓存")
        miss = [s["code"] for s in stocks if s.get("code") not in out]
    if stocks and len(out) < max(1, int(len(ids) * 0.5)):
        _note_fail("个股资金")
    live_rows = {k: v for k, v in out.items() if not v.get("_from_cache")}
    if live_rows:
        _flow_cache_save(stocks=live_rows)
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
    sector_miss = not inn_lines and not out_lines
    stock_miss = not stock_flow_ok(flow)
    flow_miss = sector_miss or stock_miss

    # 1 板块资金：行业主力是否同向；缺数据不当中性放行
    if sector_miss:
        s_sec, sec_txt, sec_mark = 40, "板块资金缺", "缺"
    elif any(k in inn_lines for k in flow_keys_of(line, s)) or (n_b and up_in >= 2):
        s_sec = 90 if any(k in inn_lines for k in flow_keys_of(line, s)) else 82
        sec_txt = "板块流入" if any(k in inn_lines for k in flow_keys_of(line, s)) else f"{board}个股热钱同向"
        sec_mark = "同向"
    elif any(k in out_lines for k in flow_keys_of(line, s)) or (
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
    if stock_miss:
        s_main_td, s_main5 = 40, 40
        main5_mark = "5日缺"
        s_same, same_txt, same_mark = 42, "个股资金缺", "缺"
        main = main5 = xlarge = 0
    else:
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
        chase += 10
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
        "flow_miss": flow_miss, "sector_miss": sector_miss, "stock_miss": stock_miss,
        "marks": {
            "板块资金": sec_mark, "资金合成": same_mark, "主力5日": main5_mark,
            "游资弹性": elast_mark, "涨停空间": room_mark, "竞价质量": auc_mark,
            "低位启动": low_mark, "承接质量": hold_mark, "追高罚": chase_mark,
        },
    }


# 自选每只票对口的东财行业/概念。不写死 BK。refresh_stock_buckets 填。
STOCK_HY = {}
FLOW_KEEP = set()
_PIN_BKS = []
SECTOR_CATALOG_PATH = os.path.join(ROOT, "reports", "sector_catalog.json")
STOCK_HY_PATH = os.path.join(ROOT, "reports", "stock_hy.json")


def _concepts_of(raw):
    s = str(raw or "").replace("，", ",")
    return [c.strip() for c in s.split(",") if c and str(c).strip() and not str(c).strip()[:1].isdigit()]


def _pick_primary(board, line, hy, concepts):
    """自选手写板块 → 东财真实资金桶。不写死 BK。
    1) 概念名等于板块或主线  2) 最短的、包含板块/主线的概念
    3) 东财行业  4) 主线名。紫金行业=工业金属，不并进有色金属。"""
    board = (board or "").strip()
    line = (line or "").strip()
    hy = (hy or "").strip()
    cons = [c for c in (concepts or []) if c]
    for c in cons:
        if c == board or c == line:
            return c
    hits = []
    for key in (board, line):
        if len(key) < 2:
            continue
        for c in cons:
            if key in c:
                hits.append(c)
    if hits:
        return min(hits, key=lambda x: (len(x), x))
    if hy:
        return hy
    return line or board or ""


def stock_primary(s):
    if not s:
        return None
    info = STOCK_HY.get(s.get("code") or "") or {}
    p = (info.get("primary") or "").strip()
    return p or None


def _catalog_page(fs, pn, hosts):
    for host in hosts:
        try:
            d = http(
                f"{host}?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12&fs={fs}&fields=f12,f14",
                timeout=12,
            )
            rows = _em_diff(d)
            total = ((d or {}).get("data") or {}).get("total")
            if rows:
                return rows, total
        except Exception:
            continue
    return [], None


def _catalog_fetch(fs):
    names = {}
    hosts = em_flow_hosts("/api/qt/clist/get")
    for pn in range(1, 12):
        rows, total = _catalog_page(fs, pn, hosts)
        if not rows:
            break
        for x in rows:
            n = str(x.get("f14") or "").strip()
            bk = str(x.get("f12") or "").strip()
            if n and bk:
                names[n] = bk
        if total and len(names) >= int(total):
            break
    return names


def sector_catalog():
    """东财行业(t:2)+概念(t:3)名称→BK。当日缓存，不写死代码。"""
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    try:
        blob = json.load(open(SECTOR_CATALOG_PATH, encoding="utf-8"))
        if blob.get("date") == today and blob.get("hy") and blob.get("gn"):
            return {"hy": blob["hy"], "gn": blob["gn"]}
    except Exception:
        pass
    hy = _catalog_fetch("m:90+t:2")
    gn = _catalog_fetch("m:90+t:3")
    if hy or gn:
        try:
            os.makedirs(os.path.dirname(SECTOR_CATALOG_PATH), exist_ok=True)
            json.dump(
                {"date": today, "hy": hy, "gn": gn},
                open(SECTOR_CATALOG_PATH, "w", encoding="utf-8"),
                ensure_ascii=False,
            )
        except Exception:
            pass
    return {"hy": hy or {}, "gn": gn or {}}


def _stock_hy_fetch(stocks):
    """ulist 的 f127/f129 是数字垃圾，必须 stock/get 拿行业/概念字符串。"""
    out = {}
    items = [s for s in (stocks or []) if s.get("code") and s.get("asset") != "etf"]
    if not items:
        return out

    def one(s):
        sid = em_secid(s)
        hosts = em_flow_hosts("/api/qt/stock/get")
        for host in hosts:
            try:
                d = http(f"{host}?secid={sid}&fields=f12,f14,f127,f129", timeout=10)
                data = (d or {}).get("data") or {}
                hy = str(data.get("f127") or "").strip()
                if hy[:1].isdigit():
                    hy = ""
                cons = _concepts_of(data.get("f129"))
                return s["code"], hy, cons
            except Exception:
                continue
        return s["code"], "", []

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one, s) for s in items]
        for fut in as_completed(futs):
            try:
                code, hy, cons = fut.result()
                out[code] = (hy, cons)
            except Exception:
                continue
    return out


def refresh_stock_buckets(stocks):
    """按自选实时行业/概念建独立资金桶，点名拉不进前12的细线。"""
    global STOCK_HY, FLOW_KEEP, _PIN_BKS
    today = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
    items = [s for s in (stocks or []) if s.get("code")]
    fetched = {}
    try:
        blob = json.load(open(STOCK_HY_PATH, encoding="utf-8"))
        if blob.get("date") == today and isinstance(blob.get("rows"), dict) and blob["rows"]:
            fetched = {
                c: (v.get("hy") or "", list(v.get("concepts") or []))
                for c, v in blob["rows"].items()
            }
    except Exception:
        fetched = {}
    miss = [s for s in items if s.get("code") not in fetched]
    if miss:
        got = _stock_hy_fetch(miss)
        fetched.update(got)
        if got:
            try:
                os.makedirs(os.path.dirname(STOCK_HY_PATH), exist_ok=True)
                rows = {}
                try:
                    old = json.load(open(STOCK_HY_PATH, encoding="utf-8"))
                    if old.get("date") == today:
                        rows = dict(old.get("rows") or {})
                except Exception:
                    rows = {}
                for c, (hy, cons) in fetched.items():
                    rows[c] = {"hy": hy, "concepts": cons}
                json.dump(
                    {"date": today, "rows": rows},
                    open(STOCK_HY_PATH, "w", encoding="utf-8"),
                    ensure_ascii=False,
                )
            except Exception:
                pass
    cat = sector_catalog()
    hy_map, gn_map = cat.get("hy") or {}, cat.get("gn") or {}
    STOCK_HY = {}
    keep = set()
    pins = []
    seen_bk = set()
    for s in items:
        code = s.get("code")
        hy, cons = fetched.get(code, ("", []))
        board = s.get("board") or ""
        line = line_of_board(board)
        primary = _pick_primary(board, line, hy, cons)
        STOCK_HY[code] = {
            "hy": hy, "concepts": cons, "primary": primary,
            "board": board, "line": line,
        }
        for name in (primary, hy):
            if not name:
                continue
            keep.add(name)
            bk = hy_map.get(name) or gn_map.get(name)
            if bk and bk not in seen_bk:
                seen_bk.add(bk)
                pins.append(bk)
    FLOW_KEEP = keep
    _PIN_BKS = pins
    return STOCK_HY


def _fmt_sector_row(x, lead=False):
    name = x.get("f14") or "-"
    chg = x.get("f3")
    amt = float(x.get("f62") or 0) / 1e8
    try:
        chg_s = f"{float(chg):+g}%"
    except (TypeError, ValueError):
        chg_s = "+0%"
    s = f"{name} {chg_s} 主力{amt:+.1f}亿"
    if lead:
        s += f" 领{x.get('f204')}"
    return s


def _sector_pin_rows():
    """行业前12之外，按自选真实行业/概念点名拉主力。BK 来自目录，不写死。"""
    if not _PIN_BKS:
        return []
    ut = "fa5fd1943c7b386f172d6893dbfba10b"
    hosts = em_flow_hosts("/api/qt/ulist.np/get")
    out = []
    seen = set()
    for i in range(0, len(_PIN_BKS), 18):
        chunk = _PIN_BKS[i:i + 18]
        secids = ",".join("90." + b for b in chunk)
        rows = []
        for host in hosts:
            try:
                d = http(
                    f"{host}?fltt=2&np=1&ut={ut}&secids={secids}&fields=f12,f14,f3,f62,f204",
                    timeout=12,
                )
                rows = _em_diff(d)
                if rows:
                    break
            except Exception:
                continue
        for x in rows:
            name = (x.get("f14") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(x)
    return out


def sector_flow():
    """东财行业主力。开盘只用实时；休市 live 连不上就爬 delay（昨收最新）。
    盘中不用 delay。周五缓存周一开盘后不用。
    行业前12之外再点名自选对口的行业/概念桶，细线不并进手写大类。"""
    ut = "fa5fd1943c7b386f172d6893dbfba10b"
    base = (
        "pn=1&pz=12&np=1&fltt=2&invt=2&fid=f62&fs=m:90+t:2"
        f"&fields=f14,f3,f62,f184,f204&ut={ut}"
    )
    hosts = em_flow_hosts("/api/qt/clist/get")
    inn, out = [], []
    for host in hosts:
        try:
            d = http(f"{host}?{base}&po=1", timeout=12)
            for x in _em_diff(d):
                inn.append(_fmt_sector_row(x, lead=True))
            if inn:
                break
        except Exception:
            continue
    for host in hosts:
        try:
            d2 = http(f"{host}?{base}&po=0", timeout=12)
            for x in _em_diff(d2):
                out.append(_fmt_sector_row(x))
            if out:
                break
        except Exception:
            continue
    seen = {sector_token(x) for x in inn + out}
    for x in _sector_pin_rows():
        name = (x.get("f14") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        amt = float(x.get("f62") or 0)
        if amt > 0:
            inn.append(_fmt_sector_row(x, lead=True))
        else:
            out.append(_fmt_sector_row(x))
    used_cache = False
    if not inn and not out:
        cached = _flow_cache_load(same_session=True) or {}
        inn = list(cached.get("inn") or [])
        out = list(cached.get("out") or [])
        if inn or out:
            used_cache = True
            _note_stale("板块资金用今日缓存")
    if not inn and not out:
        _note_fail("板块资金")
    if (inn or out) and not used_cache:
        _flow_cache_save(inn=inn, outf=out)
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


# Yahoo 在此环境经常 403。新浪美股/期货/沪金能通，隔夜盘面以它为主。
SINA_MAP = {
    "^DJI": ("gb_dji", "us"), "^GSPC": ("gb_inx", "us"), "^IXIC": ("gb_ixic", "us"),
    "^SOX": ("gb_sox", "us"), "^N225": ("int_nikkei", "int"), "^HSI": ("int_hangseng", "int"),
    "SMH": ("gb_smh", "us"), "XLK": ("gb_xlk", "us"), "XLF": ("gb_xlf", "us"),
    "XLE": ("gb_xle", "us"), "XLV": ("gb_xlv", "us"), "IBB": ("gb_ibb", "us"),
    "XLB": ("gb_xlb", "us"), "XLI": ("gb_xli", "us"), "XLY": ("gb_xly", "us"),
    "XLC": ("gb_xlc", "us"), "XLP": ("gb_xlp", "us"), "XLU": ("gb_xlu", "us"),
    "BOTZ": ("gb_botz", "us"), "TAN": ("gb_tan", "us"),
    "CL=F": ("hf_CL", "hf"), "BZ=F": ("hf_OIL", "hf"),
    "GC=F": ("hf_GC", "hf"), "SI=F": ("hf_SI", "hf"), "HG=F": ("hf_CAD", "hf"),
    "LITE": ("gb_lite", "us"), "COHR": ("gb_cohr", "us"),
}
SINA_EXTRA = {
    "nf_AU0": ("沪金", "nf"), "nf_AG0": ("沪银", "nf"), "nf_CU0": ("沪铜", "nf"),
}


def _sina_fetch(codes):
    if not codes:
        return {}
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    try:
        raw = _get(url, {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}, 10, 2)
        text = raw.decode("gbk", "replace")
    except Exception:
        _note_fail("新浪隔夜")
        return {}
    out = {}
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if "hq_str_" not in chunk or '="' not in chunk:
            continue
        code = chunk.split("hq_str_", 1)[1].split("=", 1)[0]
        inner = chunk.split('="', 1)[1].rstrip('"')
        if not inner:
            continue
        out[code] = inner.split(",")
    return out


def _sina_chg(code, parts, kind):
    try:
        if kind == "us" and len(parts) >= 3:
            last, chg = float(parts[1]), float(parts[2])
            prev = last / (1 + chg / 100) if chg != -100 else last
            return last, prev, chg
        if kind == "int" and len(parts) >= 4:
            last, chg = float(parts[1]), float(parts[3])
            prev = last / (1 + chg / 100) if chg != -100 else last
            return last, prev, chg
        if kind == "hf" and len(parts) >= 8:
            last, prev = float(parts[0]), float(parts[7] or 0)
            if not prev:
                return None
            return last, prev, (last / prev - 1) * 100
        if kind == "nf" and len(parts) >= 11:
            last = float(parts[8] or parts[6] or 0)
            prev = float(parts[10] or 0)
            if not last or not prev:
                return None
            return last, prev, (last / prev - 1) * 100
    except Exception:
        return None
    return None


def _kospi_bar():
    """Naver 实时 KOSPI。nv/cv 为指数×100。"""
    url = "https://polling.finance.naver.com/api/realtime?query=SERVICE_INDEX:KOSPI"
    d = json.loads(_get(url, {"User-Agent": UA, "Referer": "https://finance.naver.com/"}, 8, 2))
    row = (((d.get("result") or {}).get("areas") or [{}])[0].get("datas") or [{}])[0]
    last = float(row["nv"]) / 100.0
    chg_pt = float(row.get("cv") or 0) / 100.0
    prev = last - chg_pt
    chg = float(row.get("cr") or 0)
    if not prev:
        return None
    return last, prev, chg


def overnight_quotes_live():
    """一次拉齐美股指数/板块ETF、原油金银铜、光通信个股、国内期货。返回 yahoo符号 -> bar。"""
    codes = [v[0] for v in SINA_MAP.values()] + list(SINA_EXTRA)
    blob = _sina_fetch(codes)
    by_sym = {}
    for ysym, (scode, kind) in SINA_MAP.items():
        bar = _sina_chg(scode, blob.get(scode) or [], kind)
        if bar:
            by_sym[ysym] = bar
    extra = {}
    for scode, (name, kind) in SINA_EXTRA.items():
        bar = _sina_chg(scode, blob.get(scode) or [], kind)
        if bar:
            extra[scode] = {"name": name, "last": bar[0], "prev": bar[1], "chg": bar[2], "sym": scode}
    try:
        kbar = _kospi_bar()
        if kbar:
            by_sym["^KS11"] = kbar
    except Exception:
        _note_fail("KOSPI")
    missing = [s for s in SINA_MAP if s not in by_sym]
    if missing:
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_yahoo_bar, s): s for s in missing}
            for fut in as_completed(futs):
                s = futs[fut]
                bar = fut.result()
                if bar:
                    by_sym[s] = bar
    return by_sym, extra, ("新浪" if blob else "Yahoo")


NEWS_PATH = os.path.join(ROOT, "reports", "overnight_news.json")
NEWS_KW_POLICY = re.compile(
    r"央行|证监会|国务院|发改委|工信部|财政部|住建|公积金|降准|降息|加息|美联储|FOMC|规划|印发|利率|关税|财政|货币政策"
)
NEWS_KW_FOREIGN = re.compile(
    r"美股|纳指|纳斯达克|标普|道指|费城半导体|美联储|FOMC|华尔街|纽约|欧央行|欧洲央行|"
    r"日经|恒生|原油|WTI|布伦特|黄金|白银|Lumentum|Coherent|SpaceX|特朗普|白宫|"
    r"五角大楼|伊朗|霍尔木兹|北约|法国|英国首相|加州|沙特|美元|美债|外盘|隔夜|"
    r"英伟达|中概股|Anthropic|甲骨文|纳指走高|港美股|美股收盘|美股盘前|马斯克"
)
NEWS_KW_DOMESTIC = re.compile(
    r"央行|证监会|国务院|发改委|工信部|财政部|住建|公积金|A股|沪指|上证|深成|创业板|"
    r"北交所|沪深|两市|涨停|连板|主力资金|降准|印发|国内|多地|住建部|商务部|"
    r"解禁|限售|龙虎榜|融资余额"
)
NEWS_SRC_PREFIX = re.compile(
    r"^(财联社|每日经济新闻|证券时报|证券日报|上海证券报|中国证券报|第一财经|"
    r"同花顺|钛媒体App|钛媒体|万得资讯|万得|Wind|东方财富|新浪财经|科股宝)"
    r"\d*月?\d*日?(电|讯|播报)?[，,、:：]?"
)


def _news_title(it):
    title = (it.get("title") or it.get("stitle") or "").strip()
    return re.sub(r"<[^>]+>", "", title)


def _news_norm(t):
    s = re.sub(r"<[^>]+>", "", t or "").strip()
    s = NEWS_SRC_PREFIX.sub("", s)
    s = re.sub(r"(今日|昨日|将|据报道|据悉|最新|一览)", "", s)
    s = re.sub(r"亿元", "亿", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s


def _news_key(t):
    return _news_norm(t)[:28]


def _news_near(a, b):
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if len(short) >= 10 and short[:10] == long[:10]:
        return True
    return difflib.SequenceMatcher(None, a[:40], b[:40]).ratio() >= 0.82


def _news_uniq(xs):
    out, seen, norms = [], set(), []
    skip = re.compile(r"人气板块及个股点评|^\d+月\d+日涨停分析$|^涨停分析$")
    for t in xs:
        if not t or skip.search(t):
            continue
        nrm = _news_norm(t)
        k = nrm[:28]
        if t in seen or (k and k in seen):
            continue
        if any(_news_near(nrm, p) for p in norms):
            continue
        seen.add(t)
        if k:
            seen.add(k)
        norms.append(nrm)
        out.append(t)
    return out


def _news_pick(items, n, seen):
    out = []
    for t in items or []:
        nrm = _news_norm(t)
        k = nrm[:28] or t
        if not t or k in seen:
            continue
        if any(_news_near(nrm, s) for s in seen if len(s) >= 10):
            continue
        seen.add(k)
        if nrm:
            seen.add(nrm)
        out.append(t)
        if len(out) >= n:
            break
    return out


def _cls_sign(params):
    """财联社网页签名：参数按键排序，SHA1 后再 MD5。对照 RSSHub lib/routes/cls/utils.ts。"""
    items = sorted((k, str(v)) for k, v in params.items() if v not in (None, ""))
    qs = urllib.parse.urlencode(items)
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    return qs + "&sign=" + sign


def _cls_headline(it):
    title = re.sub(r"<[^>]+>", "", (it.get("title") or "").strip())
    if title:
        return title
    content = re.sub(r"<[^>]+>", "", (it.get("content") or "").strip())
    m = re.match(r"【([^】]+)】", content)
    if m:
        return m.group(1).strip()
    content = re.sub(r"^财联社\d+月\d+日电，?", "", content)
    return (content.split("。", 1)[0] or content)[:80].strip()


def crawl_macro_news():
    """国内+国外隔夜快讯：财联社为主，东财/新浪/同花顺/钛媒体/万得解禁补栏，标题去重。"""
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    foreign, domestic, mixed = [], [], []
    sources = []
    cls_hdr = {
        "User-Agent": UA,
        "Referer": "https://www.cls.cn/telegraph",
        "Accept": "application/json, text/plain, */*",
    }

    def news_get(url, referer, timeout=10):
        return _get(url, {"User-Agent": UA, "Referer": referer, "Accept": "*/*"}, timeout, 2)

    def take_cls(dest, extra=None, path="/api/cache", n=20):
        params = {"appName": "CailianpressWeb", "os": "web", "sv": "8.7.9"}
        if extra:
            params.update(extra)
        url = "https://www.cls.cn" + path + "?" + _cls_sign(params)
        try:
            d = json.loads(_get(url, cls_hdr, 10, 2))
            if str(d.get("errno") or 0) not in ("0", "0.0"):
                _note_fail("财联社")
                return
            data = d.get("data") or {}
            if isinstance(data, list):
                roll = data
            else:
                roll = data.get("roll_data") or []
            got = 0
            for it in roll:
                if not isinstance(it, dict):
                    continue
                t = _cls_headline(it)
                if t:
                    dest.append(t)
                    got += 1
                if got >= n:
                    break
            if got:
                sources.append("财联社")
        except Exception:
            _note_fail("财联社")

    def take_em(col, dest, n=8):
        url = (
            "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
            f"?client=web&biz=web_news_col&column={col}&order=1&needInteractData=0"
            f"&page_index=1&page_size={n}&req_trace=1&fields=code,showTime,title,mediaName"
        )
        try:
            d = json.loads(_get(url, {"User-Agent": UA, "Referer": "https://finance.eastmoney.com/"}, 10, 2))
            lst = ((d.get("data") or {}).get("list") or [])
            if lst:
                sources.append("东财")
            dest.extend(_news_title(it) for it in lst)
        except Exception:
            _note_fail("东财新闻")

    def take_sina(pageid, lid, dest, n=10):
        url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid={pageid}&lid={lid}&k=&num={n}&page=1"
        try:
            d = json.loads(_get(url, {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}, 10, 2))
            data = ((d.get("result") or {}).get("data") or [])
            if data:
                sources.append("新浪")
            dest.extend(_news_title(it) for it in data)
        except Exception:
            _note_fail("隔夜新闻")

    def take_ths(dest, n=18):
        url = (
            "https://news.10jqka.com.cn/tapp/news/push/stock/"
            f"?page=1&tag=&track=website&pagesize={n}"
        )
        try:
            d = json.loads(news_get(url, "https://news.10jqka.com.cn/"))
            lst = ((d.get("data") or {}).get("list") or [])
            got = 0
            for it in lst:
                t = _news_title(it) or (it.get("digest") or "").strip()
                t = re.sub(r"<[^>]+>", "", t)
                if t:
                    dest.append(t)
                    got += 1
                if got >= n:
                    break
            if got:
                sources.append("同花顺")
        except Exception:
            _note_fail("同花顺新闻")

    def take_tmt(dest, n=16):
        got = 0
        try:
            raw = news_get("https://www.tmtpost.com/nictation", "https://www.tmtpost.com/")
            page = raw.decode("utf-8", "replace")
            titles = re.findall(r"【<span[^>]*>([^<]{6,120})</span>】", page)
            if not titles:
                titles = re.findall(
                    r'class="title"[^>]*>[\s\S]*?<span[^>]*>([^<]{8,120})</span>', page
                )
            for t in titles:
                t = html.unescape(t).strip()
                if t:
                    dest.append(t)
                    got += 1
                if got >= n:
                    break
        except Exception:
            titles = []
        if got < 4:
            try:
                raw = news_get("https://www.tmtpost.com/feed", "https://www.tmtpost.com/")
                page = raw.decode("utf-8", "replace")
                for t in re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", page):
                    t = html.unescape(t).strip()
                    if not t or t.startswith("钛媒体"):
                        continue
                    dest.append(t)
                    got += 1
                    if got >= n:
                        break
            except Exception:
                pass
        if got:
            sources.append("钛媒体")
        else:
            _note_fail("钛媒体新闻")

    def take_wind(dest):
        """万得没有公开快讯口。解禁表走东财数据中心（和 Wind 解禁一览同一口径）。"""
        d = now.date()
        if d.weekday() >= 5:
            d = d + datetime.timedelta(days=(7 - d.weekday()))
        day = d.strftime("%Y-%m-%d")
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            "sortColumns=LIFT_MARKET_CAP&sortTypes=-1&pageSize=40&pageNumber=1"
            "&reportName=RPT_LIFT_STAGE&columns=ALL&source=WEB&client=WEB"
            f"&filter=(FREE_DATE%3D'{day}')"
        )
        try:
            j = json.loads(_get(
                url, {"User-Agent": UA, "Referer": "https://data.eastmoney.com/dxf/detail.html"}, 10, 2
            ))
            rows = ((j.get("result") or {}).get("data") or [])
            names, total = [], 0.0
            for x in rows:
                cap = float(x.get("LIFT_MARKET_CAP") or 0)  # 万元
                if cap <= 0:
                    continue
                total += cap
                nm = (x.get("SECURITY_NAME_ABBR") or "").strip()
                if nm and len(names) < 3:
                    names.append(f"{nm}{cap / 1e4:.2f}亿元".replace(".00亿元", "亿元"))
            yi = total / 1e4
            if yi <= 0:
                return
            dest.append(f"A股限售股解禁一览：{yi:.2f}亿元市值限售股今日解禁")
            if names:
                dest.append("今日解禁市值居前：" + "、".join(names))
            sources.append("万得")
        except Exception:
            _note_fail("万得解禁")

    cls_all, cls_watch = [], []
    take_cls(cls_all, {"name": "telegraph"}, "/api/cache", 32)
    take_cls(foreign, {"category": "hk_us", "refresh_type": "1", "rn": "24"}, "/v1/roll/get_roll_list", 18)
    take_cls(cls_watch, {"category": "watch", "refresh_type": "1", "rn": "16"}, "/v1/roll/get_roll_list", 12)

    def split_pool(items, default_dom=False):
        for t in items:
            if NEWS_KW_FOREIGN.search(t):
                foreign.append(t)
            elif NEWS_KW_DOMESTIC.search(t) or NEWS_KW_POLICY.search(t):
                domestic.append(t)
            elif default_dom:
                domestic.append(t)

    split_pool(cls_all, default_dom=False)
    split_pool(cls_watch, default_dom=True)
    mixed = []
    take_em(350, mixed, 12)
    take_em(344, mixed, 8)
    take_em(351, foreign, 12)
    take_em(357, mixed, 8)
    take_sina(153, 2516, mixed, 16)
    take_sina(153, 2518, foreign, 14)
    take_sina(153, 2515, foreign, 12)
    take_ths(mixed, 18)
    take_tmt(mixed, 16)
    take_wind(mixed)
    split_pool(mixed, default_dom=False)

    domestic, foreign = _news_uniq(domestic), _news_uniq(foreign)

    def pin(xs, pat):
        hit, rest = [], []
        for t in xs:
            (hit if pat.search(t) else rest).append(t)
        return hit + rest

    domestic = pin(domestic, re.compile(r"解禁|限售股"))
    policy = _news_uniq([t for t in domestic + foreign if NEWS_KW_POLICY.search(t)])
    src = "、".join(dict.fromkeys(sources)) or "新浪财经滚动"
    out = {
        "asof": now.strftime("%Y-%m-%d %H:%M"),
        "source": src,
        "foreign": foreign[:14],
        "domestic": domestic[:14],
        "policy": policy[:8],
        "market": foreign[:14],
    }
    try:
        os.makedirs(os.path.dirname(NEWS_PATH), exist_ok=True)
        json.dump(out, open(NEWS_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    except Exception:
        pass
    return out


def load_macro_news():
    try:
        blob = json.load(open(NEWS_PATH, encoding="utf-8"))
        ts = blob.get("asof") or ""
        saved = datetime.datetime.strptime(ts[:16], "%Y-%m-%d %H:%M")
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        if (now - saved).total_seconds() > 20 * 3600:
            return None
        return blob
    except Exception:
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


def _load_ovn_cache(max_hours=18):
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
        ("^N225", "日经225"), ("^HSI", "恒生"), ("^KS11", "KOSPI"),
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
    live, extra_cn, source = overnight_quotes_live()
    by_sym = {}
    for s, n in all_specs:
        bar = live.get(s)
        if not bar:
            continue
        last, prev, chg = bar
        by_sym[s] = {"sym": s, "name": n, "last": last, "prev": prev, "chg": chg}

    def fmt_row(row):
        if row["sym"] == "^TNX":
            return f"{row['name']} {row['last']:.3f}% {row['chg']:+.1f}bp"
        if row["name"] in ("WTI原油", "布伦特", "黄金", "白银", "铜", "美元指数", "沪金", "沪银", "沪铜"):
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
    for row in extra_cn.values():
        metals.append(row)
    metals.sort(key=lambda r: -r["chg"])
    related = pack(related_specs)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))

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
        "source": source,
        "n_quotes": len(by_sym),
        "fetched": now.strftime("%Y-%m-%d %H:%M"),
        "news": crawl_macro_news(),
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
    ("医疗研发外包", "CRO"), ("创新药", "创新药"), ("CRO", "CRO"),
    ("化学制药", "化学制药"), ("医疗服务", "医疗"),
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


def flow_keys_of(line, stock=None):
    """细线对应的东财资金名。有独立桶就只看这只票自己的行业/概念，不写死别名。"""
    p = stock_primary(stock) if stock else None
    if p:
        return (p,)
    return (line,) if line else ()


def line_of_board(board):
    return BOARD_LINE.get(board, board or "其他")


def overnight_follow_desk(ovn, stocks, etfs, rows, etf_rows, yz_by_code, inn, outf, inn_lines, out_lines):
    """隔夜主题 → 次日国内关注板块 → 自选龙头预案。只写最前一节最新资讯，不进买点闸/表一/7a。"""
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


def overnight_news_lines(now, ovn_scan):
    """最新资讯独立目录：外盘+快讯+次日预案。只盯不开仓，不进第0节闸。"""
    lines = []
    ovn_blk = MACRO.get("overnight_external") or {}
    news = (sc := (ovn_scan or {})).get("news") or load_macro_news() or {}
    lines.append("## 最新资讯（外盘+快讯+次日预案；只盯不开仓）")
    lines.append(
        "外盘报价、商品、快讯和次日映射写在这里，单独一节、放在最前。"
        "只盯不改买点闸，不进表一分/7a，也不跟尾盘隔夜仓混仓。"
    )
    src = sc.get("source") or ""
    fetched = sc.get("fetched") or news.get("asof") or ""
    nq = sc.get("n_quotes") or 0
    lines.append(f"- 抓取 {fetched or '—'} · 报价源 {src or '—'} · {nq}条 · 快讯 {(news.get('source') or '—')}")
    lines.append(f"- 隔夜偏好：**{sc.get('bias') or '-'}** — {sc.get('bias_why') or ''}")
    if sc.get("_from_cache"):
        lines.append("- 报价来源：缓存（实时未拉满，用上一份有效隔夜）")
    if sc.get("indices"):
        lines.append(
            "- 指数："
            + "；".join(f"{r['name']} {r['last']:.2f} {r['chg']:+.2f}%" for r in sc["indices"])
        )
    if sc.get("sectors"):
        top_s = sc["sectors"][:6]
        bot_s = list(reversed(sc["sectors"][-4:])) if len(sc["sectors"]) >= 4 else []
        lines.append("- 美股板块领涨：" + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in top_s))
        if bot_s:
            lines.append("- 美股板块领跌：" + "；".join(f"{r['name']} {r['chg']:+.2f}%" for r in bot_s))
    if sc.get("metals"):
        lines.append(
            "- 黄金/期货："
            + "；".join(
                f"{r['name']} {r['last']:.2f}({r['chg']:+.2f}%)"
                if r.get("sym") != "^TNX"
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
    frn = news.get("foreign") or news.get("market") or []
    domn = news.get("domestic") or []
    pol = news.get("policy") or []
    news_seen = set()
    pol_show = _news_pick(pol, 6, news_seen)
    frn_show = _news_pick(frn, 10, news_seen)
    dom_show = _news_pick(domn, 10, news_seen)
    if pol_show:
        lines.append("- **政策**")
        for x in pol_show:
            lines.append("- " + x)
    if frn_show:
        lines.append("- **国外快讯**")
        for x in frn_show:
            lines.append("- " + x)
    if dom_show:
        lines.append("- **国内快讯**")
        for x in dom_show:
            lines.append("- " + x)
    if not (pol_show or frn_show or dom_show):
        lines.append("- 快讯暂缺")
    note_asof = str(MACRO.get("asof") or "")
    today_s = now.strftime("%Y-%m-%d")
    if note_asof and note_asof < today_s and ovn_blk.get("implication"):
        lines.append(f"- 背景笔记截至 {note_asof}：" + ovn_blk.get("implication"))
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
    lines.append("### 次日关注个股")
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
    lines.append("- 用法：早上8点先看本节定关注名单；9:30后用第0节买点+竞价+资金主线确认，最新资讯不能单独开仓。")
    lines.append("")
    return lines


def _flow_alias(tok):
    """东财板块名 → 主线名。自选对口的细行业/概念留原名，不并进有色/医药等大类。"""
    tok = (tok or "").strip()
    if not tok:
        return tok
    if tok in FLOW_KEEP:
        return tok
    for k, line in FLOW_LINE:
        if k in tok:
            return line
    return tok


def flow_sets(inn, outf):
    inn_lines, out_lines = [], []
    for row in inn:
        line = _flow_alias(sector_token(row))
        if line and line not in inn_lines:
            inn_lines.append(line)
    for row in outf:
        line = _flow_alias(sector_token(row))
        if line and line not in out_lines:
            out_lines.append(line)
    return inn_lines, out_lines


# 东财流出里常见、但不是自选主线的行业。只记资金流出，不进「回避主线」。
NOISE_OUT = {
    "建筑材料", "建材", "水泥",
    "装修装饰", "房地产开发", "房地产",
}


def desk_lines_of(stocks, etfs=None):
    """自选股票/ETF 对得上的主线名。有独立桶用独立桶，不再把手写有色/医药当成资金名。"""
    lines = []
    for s in stocks or []:
        p = stock_primary(s)
        ln = p or line_of_board((s or {}).get("board"))
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
            line = _flow_alias(tok)
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
            f"https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}",
        ]
        if not cn_session_live():
            urls.append(
                f"https://push2delay.eastmoney.com/api/qt/clist/get?pn={pn}&pz={pz}&po={po}&np=1&fltt=2&invt=2&fid=f3&fs={fs}&fields={fields}"
            )
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
        phase, note = "退潮", "昨板today亏钱，打板空仓；游资只做低位且7a≥75；趋势不追热"
    elif n_zt >= 80 and (lad.get("high") or 0) >= 5:
        phase, note = "高潮", "跟风可看，不追高标、不接最高板"
    elif n_zt >= 45 and n_lian >= 6 and (prem is None or prem >= 0):
        phase, note = "发酵", "梯队在长，首板/一进二是主战场"
    elif n_zt < 20 or n_dt >= max(15, n_zt):
        phase, note = "退潮", "打板空仓；游资抬高门槛只做低位转强；趋势不追热"
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
        today = session_date()
        GATE_PREV = blob.get("codes") or {} if blob.get("date") == today else {}
    except Exception:
        GATE_PREV = {}


def gate_state_save():
    try:
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
        os.makedirs(os.path.dirname(GATE_PATH), exist_ok=True)
        json.dump(
            {"date": session_date(now), "time": now.strftime("%H:%M"), "codes": GATE_NOW},
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
    rec = {
        "call": out_call, "raw": call, "marginal": bool(marginal),
        "score": round(score or 0, 1),
    }
    kind = (GATE_NOW.get(code) or {}).get("kind") or (GATE_PREV.get(code) or {}).get("kind")
    if kind:
        rec["kind"] = kind
    GATE_NOW[code] = rec
    return out_call, out_why


# ---------- 信号留档与复盘：闭环的那一环 ----------
JOURNAL_DIR = os.path.join(ROOT, "reports", "journal")


def session_date(now=None):
    """A股交易日：周末/开盘前记到上一交易日，避免周六日复跑当成新信号。节假日未单独剔。"""
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    d = now.date()
    if now.hour < 9 or (now.hour == 9 and now.minute < 15):
        d = d - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d = d - datetime.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


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
    """可小仓/可尾盘：每个交易日每只票每种结论只记第一次入选（入选日+入选价）。
    其他结论每天每种只记一次，给闸区分度对照，不进筛选胜率表。"""
    date_s = session_date(now)
    time_s = now.strftime("%H:%M")
    if cn_session_closed(now) and time_s < "15:00":
        time_s = "15:01"
    old = journal_load(1)
    have_pick = {
        (r.get("date"), r.get("code"), r.get("call"))
        for r in old if r.get("call") in ("可小仓", "可尾盘")
    }
    have = {(r.get("date"), r.get("code"), r.get("call")) for r in old}
    new = []
    for r in rows:
        code = r.get("code")
        call = r.get("call")
        if not code or not call:
            continue
        if call in ("可小仓", "可尾盘"):
            if (date_s, code, call) in have_pick:
                continue
            have_pick.add((date_s, code, call))
        key = (date_s, code, call)
        if key in have:
            continue
        have.add(key)
        new.append({
            "date": date_s, "time": time_s, "code": code, "name": r.get("name"),
            "kind": r.get("kind"), "call": call, "line": r.get("line"),
            "st": r.get("st"), "worth": round(r.get("score") or 0, 1),
            "tape": round(r.get("tape") or 0, 1), "ma": round(r.get("ma") or 0, 1),
            "px": r.get("px"), "chg": r.get("chg"), "auc": r.get("auc"),
            "sl": r.get("sl"), "tp1": r.get("tp1"), "tp2": r.get("tp2"),
            "pick": call == "可小仓",
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


_SLTP_MD_RE = re.compile(
    r"(\d+\.\d+)\s*\(\s*[+-]?\d+(?:\.\d+)?%\s*\)\s*\|\s*"
    r"(\d+\.\d+)\s*\(\s*[+-]?\d+(?:\.\d+)?%\s*\)\s*/\s*(\d+\.\d+)"
)
_REPORT_SLTP_CACHE = {}


def _sltp_from_md(text, name):
    """从当时报告第0节表抠止损/止盈。入选当时的纪律价，不是事后改的。"""
    if not text or not name:
        return None
    for line in str(text).splitlines():
        if name not in line:
            continue
        if "可小仓" not in line and "可尾盘" not in line:
            continue
        m = _SLTP_MD_RE.search(line)
        if m:
            return float(m.group(1)), float(m.group(2)), float(m.group(3))
    return None


def _sltp_from_report(date_s, time_s, name):
    key = (date_s, time_s, name)
    if key in _REPORT_SLTP_CACHE:
        return _REPORT_SLTP_CACHE[key]
    ds = str(date_s or "").replace("-", "")
    ts = str(time_s or "").replace(":", "")
    if len(ds) != 8 or len(ts) < 3:
        _REPORT_SLTP_CACHE[key] = None
        return None
    path = os.path.join(ROOT, "reports", f"{ds}_{ts}.md")
    got = None
    try:
        if os.path.isfile(path):
            got = _sltp_from_md(open(path, encoding="utf-8").read(), name)
    except Exception:
        got = None
    _REPORT_SLTP_CACHE[key] = got
    return got


def journal_backfill_exits():
    """老记录没存止盈止损：用入选当时那份报告补上，写回 journal。"""
    recs = journal_load(3)
    by_file = {}
    changed = 0
    for r in recs:
        fn = _journal_file(r.get("date") or "")
        by_file.setdefault(fn, []).append(r)
        if r.get("call") not in ("可小仓", "可尾盘"):
            continue
        if r.get("sl") is not None:
            continue
        ex = _sltp_from_report(r.get("date"), r.get("time"), r.get("name"))
        if not ex:
            continue
        r["sl"], r["tp1"], r["tp2"] = ex
        changed += 1
    if not changed:
        return 0
    for fn, rows in by_file.items():
        try:
            os.makedirs(os.path.dirname(fn), exist_ok=True)
            with open(fn, "w", encoding="utf-8") as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        except Exception:
            continue
    return changed


def _bar_on(hist, date_s):
    for b in hist or []:
        if b[0] == date_s:
            return b
    return None


def _bar_nexts(hist, date_s):
    return [b for b in (hist or []) if b[0] > date_s]


def _day_done(now, date_s):
    """日K收盘是否已经定格。盘中当天那根K的收盘还是现价，不能当次日收。
    A股 15:00 收盘，跟 cn_session_closed 对齐；不要拖到 15:05，否则 15:00 那轮快照永远待收盘。"""
    today = now.strftime("%Y-%m-%d")
    if date_s < today:
        return True
    if date_s > today:
        return False
    return cn_session_closed(now)


def _fwd_from(hist, date_s, px, now=None):
    """信号当时的价 → 之后第1/3/5个交易日收盘。用已经抓下来的日线，不额外请求。"""
    if not hist or not px:
        return {}
    now = now or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    nxt = [b for b in _bar_nexts(hist, date_s) if _day_done(now, b[0])]
    if not nxt:
        return {}
    out = {}
    for tag, step in (("r1", 0), ("r3", 2), ("r5", 4)):
        if step < len(nxt):
            out[tag] = (nxt[step][4] / px - 1) * 100
    return out


def _pct(a, b):
    if not a or not b:
        return None
    return (a / b - 1) * 100


def _hm_min(s):
    try:
        hh, mm = str(s or "00:00").split(":")[:2]
        return int(hh) * 60 + int(mm)
    except Exception:
        return 0


def _buy_track(recs, hist_by_code, now, cost_pct=0.1, call="可小仓", exits_now=None):
    """筛选胜率：每个交易日每只指定结论只一行。
    可小仓：盘中 09:30-14:50 入选 → 成交价=入选价；收盘后/盘前 → 次日开。
    可尾盘：14:30 后记的就是当日尾盘价，不再改成次日开。
    A股T+1，胜负看次日收相对成交价扣成本。隔夜=次日开相对入选价。
    止盈止损用入选当时记下的价；当日若还没入档，用这一轮纪律价补上。
    入选次数=这只票在表里出现的天数（每天第一次算一次）。"""
    first = {}
    exits_now = exits_now or {}
    session_s = session_date(now)
    for r in recs:
        if r.get("call") != call or not r.get("code") or not r.get("date"):
            continue
        key = (r["date"], r["code"])
        old = first.get(key)
        if not old or str(r.get("time") or "") < str(old.get("time") or ""):
            first[key] = r
    n_pick = {}
    nth_of = {}
    by_code_dates = {}
    for date_s, code in first:
        by_code_dates.setdefault(code, []).append(date_s)
    for code, dates in by_code_dates.items():
        uniq = sorted(set(dates))
        n_pick[code] = len(uniq)
        for i, d in enumerate(uniq, 1):
            nth_of[(d, code)] = i
    rows = []
    n_open = n_open_win = 0
    n_fill = n_fill_win = 0
    s_open = s_fill = 0.0
    for r in sorted(first.values(), key=lambda x: (x.get("date") or "", x.get("time") or ""), reverse=True):
        try:
            px = float(r["px"]) if r.get("px") else None
        except Exception:
            px = None
        hist = hist_by_code.get(r.get("code")) or []
        nxts = _bar_nexts(hist, r["date"])
        nxt = nxts[0] if nxts else None
        nxt_open = nxt[1] if nxt else None
        nxt_done = bool(nxt and _day_done(now, nxt[0]))
        nxt_close = nxt[4] if nxt_done else None
        hm = _hm_min(r.get("time"))
        if call == "可尾盘":
            live = hm >= 14 * 60 + 30
        else:
            live = 9 * 60 + 30 <= hm <= 14 * 60 + 50
        try:
            ds = datetime.datetime.strptime(str(r.get("date") or ""), "%Y-%m-%d")
            if ds.weekday() >= 5:
                live = False
        except ValueError:
            pass
        if live:
            entry, how = px, "盘中价"
        else:
            entry, how = (nxt_open, "次日开") if nxt_open else (None, "待次日开")
        overnight = _pct(nxt_open, px)
        fill_pct = _pct(nxt_close, entry)
        net = (fill_pct - cost_pct) if fill_pct is not None else None
        result = "待次日"
        if nxt_open and not nxt_done:
            result = "待收盘"
        if not live and not nxt_open:
            result = "待次日开"
        if net is not None:
            result = "胜" if net > 0 else "负"
            n_fill += 1
            n_fill_win += 1 if net > 0 else 0
            s_fill += net
        if overnight is not None:
            n_open += 1
            n_open_win += 1 if overnight > 0 else 0
            s_open += overnight
        sl, tp1, tp2 = r.get("sl"), r.get("tp1"), r.get("tp2")
        if sl is None and exits_now:
            ex = exits_now.get(r.get("code")) or exits_now.get(r.get("name")) or {}
            if r.get("date") == session_s:
                sl = ex.get("sl")
                tp1 = tp1 if tp1 is not None else ex.get("tp1")
                tp2 = tp2 if tp2 is not None else ex.get("tp2")
        if sl is None:
            exr = _sltp_from_report(r.get("date"), r.get("time"), r.get("name"))
            if exr:
                sl, tp1, tp2 = exr[0], tp1 if tp1 is not None else exr[1], tp2 if tp2 is not None else exr[2]
        rows.append({
            "date": r.get("date"), "time": r.get("time"),
            "code": r.get("code"), "name": r.get("name"),
            "kind": r.get("kind"), "line": r.get("line"),
            "px": px, "entry": entry, "how": how,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "n_pick": n_pick.get(r.get("code")) or 1,
            "nth": nth_of.get((r.get("date"), r.get("code"))) or 1,
            "nxt_open": nxt_open, "open_pct": overnight,
            "nxt_close": nxt_close, "net_close": net, "result": result,
        })
    return {
        "rows": rows[:40],
        "n": len(rows),
        "n_open": n_open, "win_open": (n_open_win / n_open * 100) if n_open else None,
        "avg_open": (s_open / n_open) if n_open else None,
        "n_close": n_fill, "win_close": (n_fill_win / n_fill * 100) if n_fill else None,
        "avg_close": (s_fill / n_fill) if n_fill else None,
        "cost_pct": cost_pct,
    }


def journal_review(hist_by_code, days=30, cost_pct=0.1, exits_now=None):
    """按判别分桶算胜率和平均收益。这张表是用来改阈值的依据，不参与今天的闸。"""
    try:
        journal_backfill_exits()
    except Exception:
        pass
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = now.strftime("%Y-%m-%d")
    since = (now - datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    all_recs = [r for r in journal_load(3) if since <= (r.get("date") or "")]
    gate_recs = [r for r in all_recs if r.get("call") != "可尾盘"]
    recs = [r for r in gate_recs if (r.get("date") or "") < today]
    buckets = {}
    per_kind = {}
    n_eval = 0
    for r in recs:
        hist = hist_by_code.get(r.get("code"))
        if not hist:
            continue
        fwd = _fwd_from(hist, r["date"], r.get("px"), now)
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
        "buy_track": _buy_track(gate_recs, hist_by_code, now, cost_pct, exits_now=exits_now),
        "meal_track": _buy_track(all_recs, hist_by_code, now, cost_pct, call="可尾盘", exits_now=exits_now),
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


def verdict_trend(s, q, f, yld, st, mood=None):
    """趋势仓最终买点。不改 name_call。过线后加滞后带，避免同日反复改口。
    情绪退潮时不追偏热，干净的均线+均价结构仍可小仓。"""
    call, why = name_call(s, q, f, yld)
    if call == "可小仓" and st == "回避":
        return "观察", "主线回避"
    oh = overheat(s["code"], q.get("chg"), q.get("name"))
    if call == "可小仓" and (mood or {}).get("phase") == "退潮" and oh in ("偏热", "不追"):
        call, why = "观察", "情绪退潮，趋势也不追热"
    ent = (f or {}).get("entry")
    if call in ("可小仓", "观察") and ent:
        return hysteresis(s["code"], call, why, ent, 55, 48)
    return call, why


def verdict_youzi(s, q, f, yld, yz, st, mood, late):
    """游资仓最终买点。和总判同一套闸，表一看这一列就能下结论。
    换手/量比改用时段归一值：10:00 的 5% 换手按全天折算才和 14:30 的 5% 可比。
    今涨 0.70 板幅改为观察等回踩，只有见顶/涨停才硬不追。
    退潮不关死：门槛抬到 75，且只做低位、不追热。"""
    how = (yz or {}).get("how") or ""
    sc = (yz or {}).get("score") or 0
    if yday_dt_shape(q, yld) == "trap":
        return "不买", "昨跌停骗炮"
    oh = overheat(s["code"], q["chg"], q.get("name"))
    if oh in ("涨停", "见顶"):
        return "不追", f"今涨{q['chg']:+.1f}% {oh}，不追"
    if limit_open_dump(s, q):
        return "不买", "竞价涨停开后砸盘，出货不做"
    if any(k in how for k in SKIP_HOW):
        return "不买", how
    if st == "回避":
        return "观察", "主线回避"
    if late:
        return "观察", "尾盘/收盘后不新开"
    need = youzi_enter_need(yz, mood)
    hold = need - 5
    if sc < need:
        miss = "；资金数据缺" if (yz or {}).get("flow_miss") else ""
        tide = "；情绪退潮抬门槛" if (mood or {}).get("phase") == "退潮" else ""
        return hysteresis(s["code"], "观察", f"7a {sc:.0f}未达标(门槛{need:.0f}){miss}{tide}", sc, need, hold)
    if (mood or {}).get("phase") == "退潮":
        dd = (f or {}).get("dd")
        if oh in ("偏热", "不追"):
            return "观察", "情绪退潮不追热"
        if dd is not None and dd > -0.12:
            return "观察", "情绪退潮只做低位转强"
    if oh == "不追":
        return "观察", f"今涨{q['chg']:+.1f}% 过热，等回踩不追尖"
    hs_raw = q.get("turnover")
    hs = hs_proj(hs_raw)
    vr = vr_norm(q.get("vol_ratio") or 0)
    hs_ok = hs is not None and hs >= 5 and (hs_raw or 0) >= 1.0
    if not (hs_ok or vr >= 1.5):
        if hs is not None:
            return "观察", f"换手/量比不够（现换手{hs_raw:.1f}%、全天折算{hs:.1f}%、归一量比{vr:.2f}）"
        return "观察", "换手/量比不够"
    return hysteresis(s["code"], "可小仓", how, sc, need, hold)


def limit_price(prev, code, name=None):
    if not prev:
        return None
    return round(prev * (1 + board_limit_pct(code, name) / 100.0) + 1e-8, 2)


def daban_plan(s, q, f, hist, yz, st, line, mood, late=False, yld=False,
               zt_y=None, zt_t=None):
    """近7日打板战法。今首板默认不追；能买的只有昨首板一进二、昨烂板弱转强、龙头断板回踩。
    二进三及以上只盯不打。游资仓仍要 7a 过门槛；打板可小仓不要求 7a。"""
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
        if setup in ("二进三", "高位板回抽"):
            why = "二进三及以上只盯不打；" + ("；".join(bits[:3]) or "高度风险")
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


def overnight_meal_plan(s, q, f, yld, st, line, mood, now=None, zt_y=None, zt_t=None, hist=None, hot_lines=None):
    """尾盘隔夜仓：独立仓。尾盘确认后隔夜、次日早盘兑现。不进第0节可以买，不跟游资/趋势/打板混闸。
    两路：①隔夜强势（3%～5%、不涨停，换手5%～10%只加减分，不硬卡）②尾盘二板（昨首板今封死）。
    硬闸：均价下不做、近10日至少1次涨停、近3日资金主线、市值≥400亿、尾盘首板偷鸡不做。"""
    empty = {
        "in_pool": False, "score": 0, "setup": "非尾盘隔夜仓", "call": "观察",
        "why": "未进尾盘隔夜仓池", "bits": [], "sell": "次日09:30-10:00卖：+3%止盈 / -2%止损 / 10:00前清完",
    }
    if not s or not q:
        return empty
    code, name = s["code"], s.get("name") or ""
    if s.get("asset") == "etf" or "ETF" in name:
        return empty
    if is_st(code, name) or code.startswith(("8", "4", "920", "688")):
        return {
            **empty, "in_pool": True, "call": "不买",
            "why": "ST/北交所/科创不做尾盘隔夜仓",
        }
    phase = overnight_meal_phase(now)
    chg = q.get("chg") or 0
    px, high, vwap = q.get("px"), q.get("high"), q.get("vwap")
    hs = q.get("turnover")
    vr = q.get("vol_ratio") or 0
    yi = mcap_yi(q)
    cap = board_limit_pct(code, name)
    today_zt = is_limit_up(code, chg, name)
    yday = yday_zt(code, hist)
    yq = zt_quality((zt_y or {}).get(code))
    tq = zt_quality((zt_t or {}).get(code)) if today_zt else {}
    n_lian = consec_zt(code, hist)
    if (zt_y or {}).get(code):
        yday = True
        n_lian = max(n_lian, yq.get("lbc") or 1)
    bits = []
    score = 48
    setup = "隔夜观察"
    dump = bool(high and px and px < high * 0.97)
    above_vwap = vwap is None or (px is not None and px >= vwap)
    near_high = bool(high and px and px >= high * 0.985)

    if yday_dt_shape(q, yld) == "trap":
        return {**empty, "in_pool": True, "call": "不买", "why": "昨跌停骗炮，尾盘隔夜仓不做", "bits": ["骗炮"]}
    if limit_open_dump(s, q):
        return {**empty, "in_pool": True, "call": "不买", "why": "竞价涨停开后砸盘，不做隔夜", "bits": ["开后砸"]}
    if (mood or {}).get("phase") == "退潮":
        return {**empty, "in_pool": True, "call": "观察", "why": "情绪退潮，尾盘隔夜仓空仓", "bits": ["退潮"]}
    if st == "回避":
        return {**empty, "in_pool": True, "call": "观察", "why": "主线回避，尾盘隔夜仓不跟支线", "bits": ["回避"]}
    if yi is None or yi >= BIG_CAP_YI:
        if yi is None:
            setup, why = "市值未知", "市值没取到，尾盘狙击不放行，避免权重白马混进隔夜"
        else:
            setup, why = "大盘不做", (
                f"市值{yi:.0f}亿≥{int(BIG_CAP_YI)}亿，尾盘狙击不做权重白马"
                "（隔夜+3%弹性不够，走第0节趋势仓）"
            )
        return {
            **empty, "in_pool": True, "call": "不买", "setup": setup,
            "why": why, "bits": [setup],
        }
    if vwap is not None and px is not None and px < vwap:
        return {
            **empty, "in_pool": True, "call": "不买", "setup": "均价下不做",
            "why": "均价下不做隔夜，尾盘隔夜仓要求站上分时均价",
            "bits": ["均价下"],
        }
    done_bars = strip_today(hist) if hist else []
    if len(done_bars) >= 6 and recent_zt(s, hist, n=MEAL_ZT_DAYS) < 1:
        return {
            **empty, "in_pool": True, "call": "不买", "setup": "无涨停基因",
            "why": f"近{MEAL_ZT_DAYS}日无涨停，隔夜没有游资记忆",
            "bits": [f"近{MEAL_ZT_DAYS}日无涨停"],
        }
    if not meal_line_ok(line, st, hot_lines):
        return {
            **empty, "in_pool": True, "call": "不买", "setup": "非近3日主线",
            "why": f"板块{line or '-'}不是近{MEAL_LINE_DAYS}日资金主线，尾盘隔夜仓不跟独苗支线",
            "bits": [f"非近{MEAL_LINE_DAYS}日主线"],
        }

    # 尾盘二板：昨首板、今封死、换手8-18%、不是烂板/跳水
    if yday and n_lian == 1 and today_zt:
        setup = "尾盘二板"
        score += 16
        bits.append("昨首板今封二")
        if 8 <= (hs or 0) <= 18:
            score += 12
            bits.append(f"换手{hs:.1f}%")
        elif hs is not None:
            score -= 10
            bits.append(f"换手{hs:.1f}%不在8-18")
        zbc = tq.get("zbc") if tq else yq.get("zbc")
        if (zbc or 0) >= 2:
            score -= 16
            bits.append(f"烂板开板{zbc}次")
        elif tq.get("hard") or yq.get("hard"):
            score += 8
            bits.append("封单硬")
        if dump:
            score -= 20
            bits.append("尾盘跳水")
        if not above_vwap:
            score -= 8
            bits.append("均价下")
    elif today_zt:
        setup = "尾盘首板不做"
        score -= 12
        bits.append("尾盘首板当偷鸡，尾盘隔夜仓不打")
    else:
        # 杨永兴式：不涨停、涨幅甜区、量比/换手/市值、均价上、贴近当日高
        lo, hi = (3.0, 8.0) if is_20cm(code) else (3.0, 5.0)
        if lo <= chg <= hi:
            setup = "隔夜强势"
            score += 14
            bits.append(f"涨幅{chg:.1f}%在{lo:.0f}-{hi:.0f}")
        elif chg > hi:
            setup = "隔夜过热"
            score -= 8
            bits.append(f"涨幅{chg:.1f}%>{hi:.0f}，次日易高开低走")
        else:
            bits.append(f"涨幅{chg:.1f}%动能不足")
        if vr >= 1.2:
            score += 8
            bits.append(f"量比{vr:.2f}")
        else:
            score -= 8
            bits.append(f"量比{vr:.2f}<1.2")
        if hs is not None and 5 <= hs <= 10:
            score += 10
            bits.append(f"换手{hs:.1f}%")
        elif hs is not None:
            score -= 6
            bits.append(f"换手{hs:.1f}%不在5-10")
        if yi is not None and 50 <= yi <= 200:
            score += 10
            bits.append(f"流通{yi:.0f}亿")
        elif yi is not None:
            score -= 6
            bits.append(f"市值{yi:.0f}亿不在50-200")
        if above_vwap:
            score += 8
        else:
            score -= 12
            bits.append("均价下")
        if near_high:
            score += 6
        elif dump:
            score -= 18
            bits.append("尾盘跳水")
        if st == "可做":
            score += 6
            bits.append("主线可做")

    score = int(clip(score, 0, 100))
    sell = "次日09:30-10:00卖：冲高+3%止盈；开盘-2%止损；平开无力10:00前清完。一字涨停可暂留，其余不隔第二夜。"
    ok_setup = setup in ("隔夜强势", "尾盘二板") and score >= 70 and not dump
    call, why = "观察", "；".join(bits[:4]) or "形态未进甜区"
    if phase == "wait":
        call, why = "观察", "尾盘隔夜仓窗口 14:30-14:55，现在只初筛"
    elif phase == "too_late":
        call, why = "观察", "14:57后不追尾盘脉冲"
    elif phase == "sell":
        call, why = "卖", sell
    elif phase == "off":
        call, why = "观察", "非尾盘隔夜仓买卖窗（买14:30-14:55，卖次日09:30-10:00）"
    elif phase == "buy" and ok_setup:
        call, why = "可尾盘", f"{setup}达标，{sell}"
    elif phase == "buy":
        call, why = "观察", "；".join(bits[:4]) or why

    return {
        "in_pool": True, "score": score, "setup": setup, "call": call,
        "why": why, "bits": bits, "sell": sell, "phase": phase,
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

def verdict_etf(q, yz, st="-"):
    how = (yz or {}).get("how") or ""
    sc = (yz or {}).get("score") or 0
    if "可小仓" in how and sc >= 60 and q["chg"] < 5:
        if st == "回避":
            return "观察", "主线回避"
        return "可小仓", how
    if sc >= 50:
        return "观察", how or "ETF观察"
    return "不买", how or "ETF不做"


def timing_pred(now, shapes, doable, avoid):
    clk = buy_clock(now)
    weak = any(x in ("高开低走", "低开冲高回落", "开后走弱") for x in shapes)
    bits = [
        f"时点：{clk['slot']} {clk['name']}。{clk['action']}",
        f"本窗口：游资{clk['youzi']} / 趋势{clk['trend']} / 早盘接力仓{clk['daban']} / 尾盘隔夜仓{clk['meal']}",
    ]
    t = now.hour * 60 + now.minute
    if t < 11 * 60 + 30:
        bits.append("近一个月科技/医药轮动里，上午医药相对强，科技容易冲高回落。")
        if weak:
            bits.append("指数已冲高回落，上午后半段不接飞刀，看下午主线资金有没有接力。")
    elif t < 13 * 60:
        bits.append("上午结构已定，下午开盘看资金主线是否延续。")
    elif t < 14 * 60:
        bits.append("科技若有日内反抽，多半先在13:00-14:00试；医药短线这轮往往上午强、下午开始钝化。")
    elif t < 14 * 60 + 30:
        bits.append("14:00-14:30是科技日内反抽的常见窗口；没有资金回流就只是弱修复。")
    else:
        bits.append("14:30后到尾盘不追新高，只看主线资金有没有把早盘流出收住。尾盘隔夜仓看第1节。")
    if weak:
        bits.append("大盘形态是冲高回落后的修复，时点上偏向做资金还在进的线，不因为自选科技分高就改做科技。")
    if doable:
        bits.append("资金当前认可：" + "、".join(doable) + "。只在这条线上对照筛选票。")
    if avoid:
        bits.append("资金当前回避：" + "、".join(avoid) + "。")
    return bits


TECH_LINES = {"光通信", "PCB", "半导体", "算力硬件", "算力液冷", "电子元件", "消费电子"}
MED_LINES = {"创新药", "游资医药", "CRO", "医疗", "中药", "医药", "化学制药"}


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
    也不借医药生物给创新药/医疗服务，不借有色金属给工业金属。缺自己的桶就按 0。
    别名 _heat_pts_of 给主流程用，保证批量报告和单股分析页同一套口径。"""
    heat_map = heat_map or {}
    h = heat_map.get(line) or {}
    amt = h.get("amt") or 0
    if st == "可做":
        pts = 22 + clip(amt / 4.0, 0, 12)
    elif st == "中性":
        pts = (8 + clip(amt / 6.0, 0, 6)) if h else 0
    else:
        pts = clip(amt / 8.0, -8, 0)
    tag = f"{line}/{st}"
    if amt:
        tag += f" 主力{amt:+.0f}亿"
    return pts, tag


def line_status_of(s, doable, avoid):
    """批量报告和单股分析共用这一份。ETF 走 ETF_LINE 重映射。
    资金闸看这只票自己的东财行业/概念桶，不跟手写大类连坐。"""
    line = line_of_board(s.get("board"))
    if s.get("asset") == "etf" or "ETF" in (s.get("name") or ""):
        line = ETF_LINE.get(s.get("name") or "", line or "ETF")
    money = stock_primary(s) or line
    if money in (avoid or []):
        return "回避", money
    if line == "中药" and "医药" in (doable or []):
        return "中性", line
    if money in (doable or []):
        return "可做", money
    if (not stock_primary(s)) and line in MED_LINES and line not in ("中药", "创新药", "游资医药", "CRO") and "医药" in (doable or []):
        return "可做", line
    return "中性", money


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


def _gate_check_rows(s, q, fac, yz, st, line, kind, call, why, auc, db, mood, late,
                     flow, heat_map, yld, is_etf):
    """单股分析用：把实际过闸顺序摊成 过/挡/未触发，并标出卡在哪条。"""
    q, fac, yz, auc, db = q or {}, fac or {}, yz or {}, auc or {}, db or {}
    rows, flips = [], []
    code = (s or {}).get("code") or ""
    name = q.get("name")
    chg = q.get("chg")
    px, o, prev, vwap = q.get("px"), q.get("open"), q.get("prev"), q.get("vwap")
    lim = board_limit_pct(code, name)
    oh = overheat(code, chg, name)
    dt = yday_dt_shape(q, yld)
    dump = limit_open_dump(s, q)
    phase = (mood or {}).get("phase") or "不明"
    h = (heat_map or {}).get(line) or {}
    amt, schg = h.get("amt"), h.get("chg")
    gap = ((o / prev - 1) * 100) if o and prev else None
    vr_n = vr_norm(q.get("vol_ratio") or 0)
    hs_raw = q.get("turnover")
    hs_n = hs_proj(hs_raw)
    blocked = call not in ("可小仓", "可试仓")

    def row(stt, title, detail, flip=None):
        rows.append((stt, title, detail))
        if stt == "挡" and flip:
            flips.append(flip)

    if dt == "trap":
        row("挡", "昨跌停骗炮",
            f"昨跌停，今开幅{gap:+.2f}%，现价弱于开盘/均价，按冲高回落或低开假转处理，硬不买。",
            "次日要低开翻红、站住均价、量比≥1.0，才从骗炮改成弱转强")
    elif dt == "turn":
        row("过", "昨跌停骗炮", "昨跌停但是低开翻红站住均价，算弱转强，不一票否决，仓更小。")
    elif dt == "weak":
        row("过", "昨跌停骗炮", "昨跌停次日偏弱，未构成骗炮形态，不否决，只降权。")
    else:
        row("过", "昨跌停骗炮", "昨未跌停，这条没触发。")

    hot_cut = lim * 0.70
    if oh in ("涨停", "见顶"):
        row("挡", "今涨停不追",
            f"今{chg:+.2f}% / 本板{lim:.0f}%，档位「{oh}」（≥0.92板或已封死）。今板不追。",
            "等回抽或次日竞价，不在今天封板上加仓")
    elif oh == "不追" and kind in ("游资", "打板", "ETF"):
        row("挡", "今涨过热",
            f"今{chg:+.2f}%，已到0.70板（约{hot_cut:.1f}%），游资/打板改观察等回踩，不是硬涨停。",
            f"回落到0.70板以下（约<{hot_cut:.1f}%）再谈")
    else:
        extra = f"偏热（≥0.50板），趋势不追尖" if oh == "偏热" else "正常"
        row("过", "今涨停不追", f"今{chg:+.2f}%，未到0.70板（约{hot_cut:.1f}%），档位{extra}。")

    if dump:
        row("挡", "竞价涨停开后砸盘",
            f"开幅{gap:+.2f}%接近涨停，开后现价{px}低于开盘{o}，按拉高出货，不做。",
            "要开后站稳开盘价才不算砸盘")
    elif gap is not None and gap >= lim * 0.9:
        row("过", "竞价涨停开后砸盘", f"开幅{gap:+.2f}%接近涨停，但开后还站在开盘上，未触发砸盘。")
    else:
        row("过", "竞价涨停开后砸盘", f"开幅{_n(gap, 2, '%')}，不是近板开，这条没触发。")

    if st == "回避":
        money = f"主力{amt:+.1f}亿" if amt else "主力净出"
        px_txt = f"，板块涨跌{schg:+.2f}%" if schg is not None else ""
        row("挡", "主线回避",
            f"板块「{(s or {}).get('board') or '-'}」归到主线「{line}」，当日{money}{px_txt}。"
            "闸认这条线自己的钱在出，不认跌幅，也不连坐光通信/半导体。",
            "本线主力转净流入后，结构闸还要同时过")
    elif st == "可做":
        money = f"主力{amt:+.1f}亿" if amt else "主力净进"
        row("过", "主线回避", f"主线「{line}」可做（{money}）。热门涨幅不等于主力在进，这里已经是资金确认。")
    else:
        row("过", "主线回避", f"主线「{line}」中性，未进回避名单，不否决。")

    if phase == "退潮" and kind == "打板":
        row("挡", "全市场情绪", "涨停生态是退潮，打板空仓，不新开。", "情绪走出退潮后再谈打板")
    elif phase == "退潮" and kind == "游资":
        row("参考", "全市场情绪", "退潮：游资门槛从65抬到75，且只做低位、不追热，不是一刀切关死。")
    elif phase == "退潮" and kind == "趋势" and oh in ("偏热", "不追"):
        row("挡", "全市场情绪", "退潮且这只已经偏热，趋势也不追热。", "等回踩或情绪修复")
    else:
        row("过", "全市场情绪", f"当前「{phase}」。这是涨停家数/赚钱效应，不是板块资金。")

    if kind in ("游资", "打板", "ETF") and late:
        row("挡", "尾盘/休市不新开",
            "周末、盘前或14:30后，游资和打板默认不新开。",
            "下一个交易日09:30–14:30再过闸")
    elif late:
        row("参考", "尾盘/休市不新开", "趋势仓不受14:30空仓限制，但资金口径是昨收，开盘后还要再确认。")
    else:
        row("过", "尾盘/休市不新开", "仍在盘中窗口，可以按闸排队。")

    if kind == "趋势":
        cap = trend_chg_cap(code, fac.get("atr_pct"))
        ma20 = fac.get("ma20")
        above = bool(ma20) and px and px > ma20
        below = vwap is not None and px and px < vwap
        reclaim, held = bool(fac.get("vwap_reclaim")), fac.get("vwap_held", True)
        if below and (chg or 0) < 0:
            row("挡", "分时均价", f"现价{_n(px)}在均价{_n(vwap)}下，且收绿，趋势不买。", "翻红并站回均价")
        elif below:
            row("挡", "分时均价", f"现价相对均价{((px / vwap - 1) * 100):+.2f}%，还没收回。", "站回均价且不破当日关键低")
        elif reclaim and not held:
            row("挡", "分时均价", "破均价后收回，但已经跌破当日关键低，趋势不能小仓。", "守住当日关键低再收回")
        elif reclaim:
            row("过", "分时均价", "先破均价再收回，且守住当日关键低，算有效站回。")
        else:
            vs = ((px / vwap - 1) * 100) if px and vwap else None
            row("过", "分时均价", f"现价在均价上" + (f"（{vs:+.2f}%）" if vs is not None else "") + "。")
        if not above:
            row("挡", "MA20",
                f"现价{_n(px)}未上MA20 {_n(ma20)}，均线偏弱。",
                "站上MA20并同时站上均价")
        else:
            vs20 = ((px / ma20 - 1) * 100) if px and ma20 else None
            row("过", "MA20", f"现价在MA20上" + (f"（{vs20:+.1f}%）" if vs20 is not None else "") + "。")
        if fac.get("rsi") is not None and fac["rsi"] >= 70:
            row("挡", "RSI过热", f"RSI {fac['rsi']:.1f}≥70，等回踩。", "RSI回到70以下")
        elif fac.get("rsi") is not None:
            row("过", "RSI过热", f"RSI {fac['rsi']:.1f}，未到70。")
        if chg is not None and chg >= cap:
            row("挡", "趋势今涨上限",
                f"今{chg:+.2f}%，上限约{cap:.1f}%（ATR/0.35板），过了只观察不追。",
                f"回落到{cap:.1f}%以内")
        else:
            row("过", "趋势今涨上限", f"今{_n(chg, 2, '%')}，上限约{cap:.1f}%。")
        if fac.get("vp") == "价涨资金出":
            row("挡", "价涨资金出", "价在涨、主力在出，趋势不能小仓。", "个股主力转净流入")
        else:
            row("过", "价涨资金出", f"量价标注「{fac.get('vp') or '-'}」，不是价涨资金出。")
        if vr_n < 0.8 and ((chg or 0) < 0 or (px and o and px < o)):
            row("挡", "量比", f"归一量比{vr_n:.2f}且收阴，无量不能小仓。", "量比回到1.0以上")
        elif vr_n < 1.0:
            row("挡", "量比", f"归一量比{vr_n:.2f}，趋势要≥1.0才算有量站上均价。", "量比≥1.0")
        else:
            row("过", "量比", f"归一量比{vr_n:.2f}（原始{ _n(q.get('vol_ratio'), 2) }），过1.0。")

    if kind == "游资":
        need = youzi_enter_need(yz, mood)
        sc7 = yz.get("score") or 0
        miss = "；资金缺，门槛从65抬到72" if yz.get("flow_miss") else ""
        if sc7 < need:
            row("挡", "7a门槛",
                f"7a {sc7:.0f}，本闸门槛{need:.0f}{miss}。分数不够不能因为板块热就放行。",
                f"7a到{need:.0f}（刚过线还要再确认一次）")
        else:
            row("过", "7a门槛", f"7a {sc7:.0f} ≥ {need:.0f}{miss}。")
        hs_ok = hs_n is not None and hs_n >= 5 and (hs_raw or 0) >= 1.0
        if not (hs_ok or vr_n >= 1.5):
            row("挡", "换手/量比",
                f"现换手{_n(hs_raw, 1, '%')}、全天折算{_n(hs_n, 1, '%')}、归一量比{vr_n:.2f}。"
                "要换手折算≥5%（且现换手≥1%）或量比≥1.5。",
                "量能够门槛")
        else:
            row("过", "换手/量比",
                f"现换手{_n(hs_raw, 1, '%')}、折算{_n(hs_n, 1, '%')}、量比{vr_n:.2f}，量够。")
        if phase == "退潮":
            dd = fac.get("dd")
            if oh in ("偏热", "不追"):
                row("挡", "退潮只做低位", "情绪退潮且这只已经偏热，游资不追。", "等回踩或低位转强")
            elif dd is not None and dd > -0.12:
                row("挡", "退潮只做低位",
                    f"回撤{dd * 100:.1f}%，退潮只要离前高≥12%的低位转强。",
                    "更深回撤后再转强")
            else:
                row("过", "退潮只做低位", f"回撤{_n((fac.get('dd') or 0) * 100, 1, '%')}，还在低位窗口。")

    if kind == "打板":
        setup = db.get("setup") or "非打板池"
        dscore = db.get("score") or 0
        n7, n_lian = db.get("n7") or 0, db.get("n_lian") or 0
        if is_limit_up(code, chg, name):
            row("挡", "打板今首板不追", "今天自己封住了，打板不当天追首板。", "次日看一进二/弱转强")
        elif setup in ("一进二", "弱转强") and dscore >= 75:
            row("过", "打板形态", f"{setup}，打板分{dscore:.0f}≥75，近7日{n7}板、连板{n_lian}。")
        elif setup == "龙回头" and dscore >= 80:
            row("过", "打板形态", f"龙回头，打板分{dscore:.0f}≥80。")
        elif db.get("in_pool"):
            need_s = 80 if setup == "龙回头" else 75
            row("挡", "打板形态",
                f"{setup}，打板分{dscore:.0f}（要≥{need_s}），近7日{n7}板、连板{n_lian}。{db.get('why') or ''}",
                f"形态确认且打板分到{need_s}")
        else:
            row("未触发", "打板形态", "近7日无涨停，不进打板池。这只走趋势/游资闸。")

    if kind == "ETF":
        sc7 = yz.get("score") or 0
        if sc7 >= 60 and (chg or 0) < 5 and "可小仓" in (yz.get("how") or ""):
            row("过", "ETF闸", f"盘面分{sc7:.0f}≥60，今涨{_n(chg, 2, '%')}<5%。")
        elif sc7 >= 50:
            row("挡", "ETF闸", f"盘面分{sc7:.0f}，未同时满足≥60且今涨<5%。", "分到60且涨幅压住")
        else:
            row("挡", "ETF闸", f"盘面分{sc7:.0f}<50，ETF不做。", "跟主线且分到50以上才观察")

    if "刚过线" in (why or "") or "滞后带" in (why or ""):
        row("挡" if blocked else "过", "滞后带",
            why + "。硬否决（回避/骗炮/涨停）不会走滞后带，分数刚过线才等下一次。")

    bind = next(((t, d) for stt, t, d in rows if stt == "挡"), None)
    if not bind and call in ("可小仓", "可试仓"):
        bind = (call, why or "硬闸和结构闸都过了")
    elif not bind:
        bind = ("未过闸", why or "没有单独标出挡的那条，看结构闸")
    return rows, bind, flips


def explain_analyze(s, q, fac, yz, st, line, kind, call, why, auc, db, left, ex,
                    mood, late, doable, avoid, flow, htag, sc, tape_txt, is_etf,
                    heat_map=None, yld=False):
    """单次分析的可读依据。闸结论仍用 call/why，这里把判定条件和数据摊开。"""
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

    rows, bind, flips = _gate_check_rows(
        s, q, fac, yz, st, line, kind, call, why, auc, db, mood, late,
        flow, heat_map, yld, is_etf,
    )
    bind_title, bind_detail = bind
    gate_name = {"趋势": "趋势闸", "游资": "游资闸", "打板": "打板闸", "ETF": "ETF闸"}.get(kind, "买点闸")
    flip_txt = "；".join(dict.fromkeys(flips)) if flips else ""
    if call in ("可小仓", "可试仓") and not flips:
        stuck = f"硬闸和结构闸都过了。{why}"
    else:
        stuck = f"卡在「{bind_title}」：{bind_detail}"
        if flip_txt:
            stuck += f" 要翻成可小仓：{flip_txt}。"
    add(
        "结论",
        f"{hand_of(call)}。走{gate_name}，买点「{call}」。{stuck}"
        f"值分 {_n(sc, 0)} 只在已经过闸的票里排队，不能把观察抬成可小仓。",
    )

    hard = [r for r in rows if r[1] in (
        "昨跌停骗炮", "今涨停不追", "今涨过热", "竞价涨停开后砸盘",
        "主线回避", "全市场情绪", "尾盘/休市不新开", "打板今首板不追",
    )]
    struct = [r for r in rows if r not in hard]
    add(
        "判定·硬闸",
        "一票否决，挡一条就停。\n" + "\n".join(f"{a}「{b}」{c}" for a, b, c in hard),
    )
    if struct:
        add(
            "判定·结构闸",
            f"走{gate_name}才看这些。\n" + "\n".join(f"{a}「{b}」{c}" for a, b, c in struct),
        )

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

    inn_s = "、".join(list(doable)[:8]) or "暂无净流入主线"
    desk = desk_lines_of(WL.get("stocks"), WL.get("etfs"))
    if line and line not in desk:
        desk = list(desk) + [line]
    shown_avoid = avoid_for_desk(avoid, desk)
    out_s = "、".join(list(shown_avoid)[:8]) or "暂无净流出主线（自选对口）"
    hh = (heat_map or {}).get(line) or {}
    line_amt = hh.get("amt")
    line_chg = hh.get("chg")
    flow_note = ""
    if line_amt is not None:
        flow_note = f"本线当日主力{line_amt:+.1f}亿"
        if line_chg is not None:
            flow_note += f"、涨跌{line_chg:+.2f}%"
        if line_amt < 0 and (line_chg or 0) > 0:
            flow_note += "，典型价涨资金出"
        flow_note += "。"
    add(
        "主线资金",
        f"板块「{s.get('board') or '-'}」→ 主线「{line}」，状态「{st}」（{htag}）。{flow_note}"
        f"今日可做：{inn_s}。今日回避：{out_s}。"
        "首页回避只显示自选对口线；汽车/煤炭/银行即使全市场在跌也不进这套闸。"
        "板块热度和主线可做/回避是同一笔当日主力，不是两道条件。",
    )

    main = (flow or {}).get("main")
    main5 = (flow or {}).get("main5")
    xlarge = (flow or {}).get("xlarge")
    same = yz.get("same_txt") or ""
    stock_miss = not stock_flow_ok(flow)
    add(
        "个股资金",
        ("接口没拉到今主力，按缺数据处理，不当中性放行。" if stock_miss else "")
        + f"今主力{_yi(main)}，超大单{_yi(xlarge)}，近5日主力{_yi(main5)}。"
        + (f"{same}。" if same else "")
        + f"量价标注：{fac.get('vp') or '-'}。"
        + ("今主力是加分项，主线回避仍一票否决。" if st == "回避" else ""),
    )

    tape_bits = [x for x in (
        fac.get("orb"), fac.get("pullback"), fac.get("slope_txt"),
        fac.get("yhl"), fac.get("vwap_pos"), fac.get("pos"), fac.get("rs_txt"),
    ) if x]
    add(
        "量能盘面",
        f"量比{_n(vr, 2)}（归一{vr_norm(vr or 0):.2f}），换手{_n(hs, 2, '%')}"
        f"（全天折算{_n(hs_proj(hs), 1, '%')}），振幅{_n(q.get('amp'), 2, '%')}。"
        + (("盘面：" + "；".join(tape_bits) + "。") if tape_bits else "")
        + f"买点分{_n(fac.get('entry'), 0)}，均线分{_n(fac.get('buy'), 0)}。"
        + (f"盘面列 {tape_txt}。" if tape_txt else ""),
    )

    abits = "，".join(auc.get("bits") or [])
    hold_open = px and o and px >= o
    add(
        "集合竞价",
        f"{auc.get('call') or '竞价缺'}：{auc.get('why') or '无记录'}。"
        + (f"{abits}。" if abits else "")
        + f"开幅{_n(gap, 2, '%')}，开后现价相对开盘{'站稳' if hold_open else '已弱于开盘'}。"
        + ("竞价只解释开盘形态，不单独改买点；骗炮/砸盘已经在硬闸里。" if auc else ""),
    )

    if not is_etf and yz:
        marks = yz.get("marks") or {}
        mk = "；".join(f"{a}{b}" for a, b in marks.items()) if marks else (yz.get("factor_line") or "")
        need = youzi_enter_need(yz, mood)
        add(
            "游资7a",
            f"{_n(yz.get('score'), 0)}分 / 门槛{need:.0f}，{yz.get('how') or ''}。"
            f"板块因子：{yz.get('sec_txt') or '-'}。弹性{yz.get('elast_mark') or '-'}，"
            f"距涨停还剩约{_n(yz.get('room'), 1, '%')}。"
            + (f"因子：{mk}。" if mk else "")
            + ("7a是游资仓的盘面分；这只当前不走游资闸，只作对照。" if kind != "游资" else ""),
        )

    if db and db.get("in_pool"):
        bits = "；".join(db.get("bits") or [])
        add(
            "打板战法",
            f"{db.get('setup') or ''}，打板分{_n(db.get('score'), 0)}，闸「{db.get('call') or ''}」。"
            f"{db.get('why') or ''}。近7日涨停{db.get('n7') or 0}次，连板{db.get('n_lian') or 0}。"
            + (f"细节：{bits}。" if bits else "")
            + "今首板不追；能买的是昨首板一进二、弱转强或龙回头。",
        )
    elif not is_etf:
        add("打板战法", "近7日无涨停，不进打板池。这只不走打板闸。")

    phase = (mood or {}).get("phase") or "不明"
    nzt = (mood or {}).get("n_zt")
    add(
        "情绪时点",
        f"全市场涨停情绪「{phase}」"
        + (f"（涨停{nzt}家）" if nzt else "")
        + "，和主线资金不是同一个条件。"
        + ("已过14:30或休市，游资/早盘接力仓不新开。" if late else "盘中时段，仍可按闸排队。")
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
        "先看硬闸（涨停/骗炮/砸盘/主线回避/退潮/尾盘），再看结构闸（均价、MA20、7a、换手）。"
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
        refresh_stock_buckets((WL.get("stocks") or []) + [s])
    except Exception:
        pass
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
    flow = flows.get(code)
    ran("个股资金", stock_flow_ok(flow), "缺" if not stock_flow_ok(flow) else "")
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
    late = youzi_late(now)
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
    kind = "ETF" if is_etf else session_kind(code, stock_kind(s, q, hist))
    GATE_NOW.setdefault(code, {})["kind"] = kind
    st, line = line_status_of(s, doable, avoid)
    if is_etf:
        call, why = verdict_etf(q, yz, st)
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
        call, why = verdict_trend(s, q, fac, yld, st, mood)
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
        heat_map=heat_map, yld=yld,
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
    global WL, FETCH_FAIL
    FETCH_FAIL = {}
    FLOW_STALE.clear()
    WL = load_watchlist()
    gate_state_load()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    stocks, etfs, idx = WL["stocks"], WL["etfs"], WL["index"]
    extra = [
        "hkHSI", "sh000688", "usIXIC", "usEWY",
        "sz159941", "sh513100", "sh512480", "sz159813", "sh515050",
        "sh512010", "sh000933", "sz399989",
    ]
    codes = extra[:]
    for x in idx + stocks + etfs:
        codes.append(("sh" if x["market"] == "sh" else "sz") + x["code"])
    live = tencent(codes)

    idx_lines, shapes = [], []
    idx_short = {"上证指数": "上证", "深证成指": "深成", "创业板指": "创业", "沪深300": "沪深300"}
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

    try:
        refresh_stock_buckets(stocks)
    except Exception:
        pass
    inn, outf = sector_flow()
    sector_flow_ok = bool(inn or outf)
    etf_items = []
    for e in etfs:
        ee = dict(e)
        ee["asset"] = "etf"
        ee.setdefault("board", "ETF")
        etf_items.append(ee)
    flows = stock_flow(stocks + etf_items)
    n_flow = sum(1 for x in stocks if stock_flow_ok(flows.get(x["code"])))
    try:
        ovn_scan = overnight_scan(stocks, etfs)
    except Exception:
        ovn_scan = {
            "summary": [], "indices": [], "sectors": [], "metals": [], "related": [],
            "themes": [], "leaders": [], "laggers": [], "bias": "数据暂缺",
            "bias_why": "隔夜报价拉取失败", "picks": [], "avoid": [], "oil_note": "",
            "avg_idx": 0,
        }
    if (ovn_scan.get("n_quotes") or 0) >= 5 or ovn_scan.get("indices") or ovn_scan.get("themes"):
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

    def _idx_chip(name, q=None, ovn_row=None, code=""):
        if q and q.get("px") not in (None,):
            return {
                "name": name, "code": code,
                "px": round(q["px"], 2), "chg": round(q["chg"], 2),
                "open": round(q.get("open") or 0, 2),
                "high": round(q.get("high") or 0, 2),
                "low": round(q.get("low") or 0, 2),
                "shape": index_shape(q) if q.get("prev") else "",
                "vol_ratio": round(q.get("vol_ratio") or 0, 2),
            }
        if ovn_row and ovn_row.get("last") not in (None,):
            return {
                "name": name, "code": ovn_row.get("sym") or code,
                "px": round(ovn_row["last"], 2), "chg": round(ovn_row["chg"], 2),
            }
        return {"name": name, "code": code, "px": None, "chg": None}

    ovn_idx = {r.get("name"): r for r in (ovn_scan.get("indices") or [])}
    idx_tape = []
    for x in idx:
        idx_tape.append(_idx_chip(idx_short.get(x["name"], x["name"]), live.get(x["code"]), code=x["code"]))
    idx_tape.append(_idx_chip("恒生", live.get("HSI"), ovn_idx.get("恒生"), "HSI"))
    idx_tape.append(_idx_chip("科创50", live.get("000688"), code="000688"))
    q_us = live.get(".IXIC") or live.get("IXIC")
    idx_tape.append(_idx_chip("纳指", q_us, ovn_idx.get("纳指"), "IXIC"))
    idx_tape.append(_idx_chip("日经225", None, ovn_idx.get("日经225") or ovn_idx.get("日经"), "N225"))
    idx_tape.append(_idx_chip("KOSPI", None, ovn_idx.get("KOSPI"), "KS11"))
    idx_tape.append(_idx_chip("美半", None, ovn_idx.get("费城半导体"), "SOX"))
    if not any(r.get("px") is not None for r in idx_tape):
        idx_tape = [{"name": n, "px": None, "chg": None} for n in
                    ("上证", "深成", "创业", "沪深300", "恒生", "科创50", "纳指", "日经225", "KOSPI", "美半")]


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
    news_now = (ovn_scan or {}).get("news") or load_macro_news() or {}
    metal_pick = []
    for r in (ovn_scan or {}).get("metals") or []:
        if r.get("name") in ("黄金", "沪金", "WTI原油", "布伦特", "铜", "沪铜"):
            metal_pick.append(f"{r['name']}{r['chg']:+.2f}%")
    tape_bits = []
    for r in idx_tape:
        if r["name"] in ("上证", "纳指", "日经225", "KOSPI", "美半", "恒生") and r.get("px") is not None:
            tape_bits.append(f"{r['name']}{r['chg']:+.2f}%")
    tape_bits.extend(metal_pick[:4])
    all_day_b = (ovn_scan or {}).get("day_boards") or []
    day_b = [d for d in all_day_b if d.get("in_desk")]
    if len(day_b) < 5:
        day_b = day_b + [d for d in all_day_b if d not in day_b][: 5 - len(day_b)]
    news_seen = set()
    news_bits = (
        _news_pick(news_now.get("policy"), 1, news_seen)
        + _news_pick(news_now.get("foreign"), 2, news_seen)
        + _news_pick(news_now.get("domestic"), 2, news_seen)
    )
    ovn_desk = {
        "bias": (ovn_scan or {}).get("bias") or "-",
        "bias_why": (ovn_scan or {}).get("bias_why") or "",
        "tape": "；".join(tape_bits[:8]),
        "boards": [f"{d['line']}({d.get('attitude') or '-'})" for d in day_b[:6]],
        "names": [p["name"] for p in ((ovn_scan or {}).get("dragons") or (ovn_scan or {}).get("picks") or [])[:6]],
        "news": "；".join(news_bits),
    }
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
    hot_lines = meal_hot_lines(session_date(now), doable)
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
    raw_by_code = {}
    for r in list(rows) + list(etf_rows):
        item, _q, fac, _yld, hist = r
        raw_by_code[item["code"]] = ((fac or {}).get("_raw") or hist)
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
    clk = buy_clock(now)
    time_bits.append(
        "尾盘隔夜仓单独看第1节：独立仓，14:30-14:55买、次日09:30-10:00卖，不进第0节可以买"
    )
    time_bits.append("散户T+1：" + clk["retail"])

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
    elif not sector_flow_ok:
        line_block.append("可做：资金数据暂缺，本轮不作数（不把缺数据当成中性放行）")
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

    late_youzi = youzi_late(now)
    after_close = cn_session_closed(now)
    verdicts = {}
    daban_by_code = {}
    meal_by_code = {}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        kind = session_kind(s["code"], stock_kind(s, q, hist))
        GATE_NOW.setdefault(s["code"], {})["kind"] = kind
        st, line = line_status_of(s)
        yz = yz_by_code.get(s["code"])
        db = daban_plan(s, q, f, hist, yz, st, line, mood, late_youzi, yld, zt_y, zt_t)
        if db.get("in_pool"):
            daban_by_code[s["code"]] = db
        meal = overnight_meal_plan(s, q, f, yld, st, line, mood, now, zt_y, zt_t, hist, hot_lines)
        if meal.get("in_pool"):
            meal_by_code[s["code"]] = meal
        if kind == "游资":
            call, why = verdict_youzi(s, q, f, yld, yz, st, mood, late_youzi)
        else:
            call, why = verdict_trend(s, q, f, yld, st, mood)
        if db.get("call") == "可小仓" and call != "可小仓" and call not in ("不买", "不追"):
            call, why, kind = db["call"], db["why"], "打板"
        elif db.get("call") == "可小仓" and call == "可小仓":
            why = db["why"] + "；" + why
            kind = "打板"
        verdicts[s["code"]] = (call, why, kind, line, st)
    for s, q, f, yld, yz in etf_yz:
        st, line = line_status_of(s)
        call, why = verdict_etf(q, yz, st)
        verdicts[s["code"]] = (call, why, "ETF", line, st)

    meal_phase = overnight_meal_phase(now)
    meal_date = session_date(now)
    meal_ok = []
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        m = meal_by_code.get(s["code"]) or {}
        if m.get("call") == "可尾盘":
            meal_ok.append({
                "code": s["code"], "name": s["name"], "px": q["px"], "chg": q["chg"],
                "setup": m.get("setup"), "score": m.get("score") or 0,
                "why": m.get("why") or "", "sell": m.get("sell") or "",
            })
    meal_ok.sort(key=lambda x: -x["score"])
    meal_watch_rows = []
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        m = meal_by_code.get(s["code"]) or {}
        if m.get("call") == "可尾盘":
            continue
        if (m.get("score") or 0) < 70:
            continue
        meal_watch_rows.append({
            "code": s["code"], "name": s["name"], "px": q["px"], "chg": q["chg"],
            "setup": m.get("setup"), "score": m.get("score") or 0,
            "call": m.get("call") or "观察", "why": m.get("why") or "",
        })
    meal_watch_rows.sort(key=lambda x: -x["score"])
    if meal_phase == "buy":
        overnight_meal_save(meal_date, meal_ok[:5], now)
    meal_blob = overnight_meal_load()
    meal_hold_picks = []
    bdate = meal_blob.get("date") or ""
    if meal_phase == "sell" and bdate and bdate < meal_date:
        meal_hold_picks = meal_blob.get("picks") or []
    elif meal_phase in ("off", "too_late") and bdate == meal_date:
        meal_hold_picks = meal_blob.get("picks") or []
    meal_hold_picks = [
        x for x in meal_hold_picks
        if (meal_by_code.get(x.get("code") or "") or {}).get("setup") not in ("大盘不做", "市值未知")
    ]

    t1_all, n_trend, n_youzi = [], 0, 0
    call_rank = {"可小仓": 0, "观察": 1, "不追": 2, "不买": 3}
    for s, q, f, yld, hist in rows:
        if not q:
            continue
        kind = session_kind(s["code"], stock_kind(s, q, hist))
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
        oh = overheat(s["code"], q["chg"], q.get("name"))
        if is_limit_up(s["code"], q["chg"], q.get("name")) or oh in ("涨停", "见顶"):
            trend_no.append(f"{s['name']} 今涨停/见顶不追")
            continue
        if call == "可小仓" and kind != "打板":
            trend_ok.append((s["name"], q["px"], q["chg"], f["buy"], line, st, why, f.get("entry") or 0))
        elif name_call(s, q, f, yld)[0] == "可小仓" and st == "回避":
            trend_no.append(f"{s['name']} 表一可小仓但主线回避")
    youzi_ok, youzi_no = [], []
    if mood.get("phase") == "退潮":
        youzi_no.append("情绪退潮：打板空仓；游资门槛7a≥75且只做低位，不一律关死")
    for s, q, f, yld, yz in yz_youzi:
        call, why, kind, line, st = verdicts.get(s["code"], ("观察", "", "游资", "", ""))
        if kind != "游资":
            continue
        if call == "可小仓":
            youzi_ok.append((s["name"], q["px"], q["chg"], yz["score"], line, st, why))
        elif yz["score"] >= youzi_enter_need(yz, mood) and call != "不追":
            youzi_no.append(f"{s['name']} 7a {yz['score']:.0f}分 {why}")
    if late_youzi:
        youzi_no.append(
            ("收盘后，游资/早盘接力仓不新开，这份只当次日预案" if after_close
             else "14:30后游资只续不新开（7a分不改，仍列出备选）")
        )
    etf_ok = []
    for s, q, f, yld, yz in etf_yz:
        v = verdicts.get(s["code"])
        call, why = (v[0], v[1]) if v else verdict_etf(q, yz)
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
        pick_name, pick_why = r[0], f"早盘接力仓 {daban_by_code.get(r[7], {}).get('setup') or '战法'} {r[3]:.0f} {r[4]}/{r[5]}"
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
    buy_parts = []
    if trend_ok:
        buy_parts.append("趋势 " + "、".join(x[0] for x in trend_ok))
    if daban_ok:
        buy_parts.append("早盘接力仓 " + "、".join(x[0] for x in daban_ok))
    if youzi_ok:
        buy_parts.append("游资 " + "、".join(x[0] for x in youzi_ok))
    if etf_ok:
        buy_parts.append("ETF " + "、".join(x[0] for x in etf_ok))
    buy_line = "；".join(buy_parts) if buy_parts else "没有。不开新仓"
    # 左侧可试仓本来就在第0节表里列着，却不进「可以买」，两处对不上。
    # 现在单列出来，标清是轻仓试不是过闸。
    left_names = [x.split()[0] for x in left_ok]
    if left_names:
        buy_line += f"；左侧轻仓试：{'、'.join(left_names[:4])}"
    # 第13节「综合结论」过去只用趋势池的 name_call 推，游资/打板/ETF 过闸的票抬不动它，
    # 于是同一份报告第0节说「可以买」、第13节说「不买」。现在统一用同一批过闸名单。
    if buy_bits:
        buy_today = "可小仓"
        buy_reason.append("综合结论与第0节同一套闸：过闸的是 " + "、".join(buy_bits) + "。尾盘隔夜仓只在第1节，不进这里")
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
        ex = exits_all.get(x.get("code")) or exits_all.get(x.get("name")) or {}
        if ex:
            x["sl"] = ex.get("sl")
            x["tp1"] = ex.get("tp1")
            x["tp2"] = ex.get("tp2")
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
    meal_rows = []
    seen_meal = set()
    blob_picks = meal_blob.get("picks") or [] if (meal_blob.get("date") == meal_date) else []
    for x in list(meal_ok) + list(blob_picks):
        code = x.get("code")
        if not code or code in seen_meal:
            continue
        seen_meal.add(code)
        meal_rows.append({
            "code": code, "name": x.get("name"),
            "kind": x.get("setup") or "尾盘狙击",
            "call": "可尾盘", "line": "尾盘狙击", "st": "",
            "score": x.get("score") or 0,
            "px": x.get("px"), "chg": x.get("chg"),
        })
    try:
        n_j = journal_record(worth_all, now)
        n_jm = journal_record(meal_rows, now)
    except Exception:
        n_j = 0
        n_jm = 0
    try:
        review = journal_review(raw_by_code, 30, risk_cfg.get("cost_pct") or 0.1, exits_all)
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
    lines.extend(overnight_news_lines(now, ovn_scan))
    lines.append("## 0 今日必买")
    lines.append(f"**今日必买：{buy_line}**")
    if pick_name:
        lines.append(f"**最适合买：{pick_name}（{pick_why}）**")
    else:
        lines.append("**最适合买：没有。不开新仓**")
    lines.append(f"**今日最值得买 TOP5：{top5_line}**")
    lines.append(f"**备选池（观察不进最值得买）：{alt_line}**")
    lines.append("仓怎么分：**游资≠打板。** 游资是7a短线仓（09:35–10:15）。打板有两套：早盘接力仓（昨板今接力，今首板不追）在第0节；尾盘狙击是隔夜打板，只看第1节，不进今日必买。")
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
        lines.append(f"| 早盘接力仓 | {name} | {px:.2f} | {chg:+.2f}% | **可小仓** | {htag} | {setup}·{how} | {sl} | {tp} |")
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
    lines.append("### 0b 早盘接力仓（今首板不追，昨板接力才看）")
    lines.append("- 今天刚封的**首板默认不追**。能买的只有：昨首板今天冲二（一进二）、昨烂板弱转强、龙头断板回踩（龙回头）。二进三及以上只盯不打。")
    lines.append("- 尾盘狙击不走这一仓，看 **第1节 尾盘狙击**。")
    lines.append("- 看哪里：本表上面「早盘接力仓」行；第10节打板排名里「打板闸=可小仓」；首页「今日必买」里带「早盘接力仓」的名字。")
    if daban_ok:
        lines.append("- **本轮早盘接力可小仓：** " + "、".join(
            f"{x[0]}({(daban_by_code.get(x[7]) or {}).get('setup') or '打板'})" for x in daban_ok
        ))
    else:
        lines.append("- **本轮早盘接力可小仓：没有。**")
    daban_watch = []
    for code, db in sorted(daban_by_code.items(), key=lambda kv: -kv[1].get("score", 0)):
        if db.get("call") == "可小仓":
            continue
        row = next((x for x in rows if x[0]["code"] == code), None)
        if not row or not row[1]:
            continue
        daban_watch.append(f"{row[0]['name']} {db.get('setup') or '-'} {db.get('call')} {db.get('score', 0):.0f}分")
        if len(daban_watch) >= 8:
            break
    if daban_watch:
        lines.append("- 在池未过闸：" + "；".join(daban_watch))
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
                f"| {i} | {r['name']} | {display_kind(r['kind'])} | {px} | {r['chg']:+.2f}% | **{r['call']}** | {r.get('auc') or '-'}({ap_txt}) | {r['heat']} | {r['tape_txt']} | **{r['score']:.0f}** | {r['role']} | {sl} | {tp} | {r['why']} |"
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
                f"| {i} | {r['name']} | {display_kind(r['kind'])} | {px} | {r['chg']:+.2f}% | **{r['call']}** | {r.get('auc') or '-'}({ap_txt}) | {r['heat']} | {r['tape_txt']} | **{r['score']:.0f}** | {r['role']} | {sl} | {tp} | {r['why']} |"
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
                f"| {i} | {r['name']} | {display_kind(r['kind'])} | {r['line']} | {r['px']:.{nd}f} | {r['sl']:.{nd}f} | "
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
    lines.append("- 今日必买以第0节过闸名单为准。TOP5只是可小仓里按值分谁更靠前，值分高不能推翻闸，也不能把出货票洗白。尾盘隔夜仓只在第1节，不进今日必买。")
    lines.append("- 今日必买=总闸过了才能开仓。TOP5只排可小仓（按值分）；观察/可试仓再热也只进备选池，不把TOP5凑满。值分：闸+主线热+盘面(趋势买点分/游资7a)×0.28+均线分×0.18+竞价。均线分只拉开能买里谁更稳，不能翻盘。")
    lines.append("- 值分去重：游资/打板/ETF 的盘面分里已含板块资金和竞价质量，值分里主线热度只按0.45计、竞价不再重复加（竞价列显示0即此意，判别仍照常用）；趋势用买点分，不含这两项，全额计。")
    lines.append("- 判别加了滞后带：刚过线要连续两次达标才给可小仓。降级、骗炮、回避立即生效。情绪退潮：打板空仓，游资抬门槛只做低位，趋势不追热，不再一刀切关掉游资。")
    lines.append("- 早盘接力仓：今首板不追。只做昨首板一进二、昨烂板弱转强、板内龙头回头。昨一字不打。赚钱效应差或情绪退潮时不新开。可小仓不要求 7a。")
    lines.append("- 竞价涨停/近板开后砸盘→不买（出货）。竞价质量已并入各战法盘面分；量比和换手都按时段归一（早盘成交前置，10:00的量比1.5不等于14:30的1.5）。")
    lines.append("- 表一看「买点」列：可小仓=能买，观察=盯着，不买/不追=不能买。分只是均线健康。")
    miss_q = [x["name"] for x in stocks + etfs if not live.get(x["code"])]
    if miss_q or FETCH_FAIL or FLOW_STALE:
        bits = []
        if miss_q:
            bits.append("无行情（停牌/取不到）：" + "、".join(miss_q[:8]))
        if FETCH_FAIL:
            bits.append("取数失败：" + "、".join(f"{k}×{v}" for k, v in FETCH_FAIL.items()))
        if FLOW_STALE:
            bits.append("口径备注：" + "、".join(FLOW_STALE) + "（同交易日已成功缓存，不是昨收delay）")
        lines.append("- **数据完整性**：" + "；".join(bits) + "。取数失败的票结论不可用，别当成「没信号」。")

    # ---- 1 尾盘狙击（独立仓） ----
    lines.append("## 1 尾盘狙击（独立仓：尾盘买、次日早盘卖；不进第0节今日必买）")
    lines.append(
        "跟游资/早盘接力仓/趋势分开，单独一套闸，不混仓、不进今日必买。游资14:30后不新开；尾盘隔夜仓反过来，闸窗 **14:30–14:57**，买点钟主买 **14:40–14:55**（14:30先看盘）。"
        "核心：尾盘确认资金还在、不追尾盘偷鸡首板、次日 **09:31–09:50 冲高卖、10:00前了结**。"
        "两路：①隔夜强势=今涨3%～5%（20cm到8%）、未涨停、量比≥1.2、换手5%～10%加减分、流通50～200亿加分、均价上方、贴近当日高；"
        "②尾盘二板=昨首板今封死、换手8%～18%、不是烂板/跳水、贴主线。"
        "硬闸：均价下不做；近10日至少1次涨停；近3日资金主线才做；市值≥400亿否决。ST/科创/北交所/主线回避/退潮/骗炮不做。14:57后不追脉冲。"
        "这是散户能做的隔夜套利，用来替代游资席位做不到的T+0。"
    )
    lines.append(
        f"- 当前窗口：**{meal_phase}**"
        + {"buy": "（14:30-14:55，可以下尾盘狙击；14:40后更稳）", "too_late": "（14:57后不追）",
           "sell": "（次日早盘，只卖不买）", "wait": "（等到14:30再扫）",
           "off": "（休市/盘前/收盘后，未买则错过）"}.get(meal_phase, "")
    )
    lines.append("- 卖出纪律：冲高约+3%止盈；开盘约-2%止损；平开/无力翻红 **10:00前清完**。一字涨停可暂留，其余不隔第二夜。")
    lines.append("- 跟游资「上午买下午卖」不是同一仓：游资下午卖的是当天席位T+0；散户买尾盘狙击是尾盘买、次日早盘卖。")
    if meal_phase == "buy" and meal_ok:
        lines.append("| 序 | 股票 | 战法 | 分 | 价 | 今涨 | 买点 | 为什么 | 次日怎么卖 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for i, r in enumerate(meal_ok[:6], 1):
            lines.append(
                f"| {i} | {r['name']} | {r['setup']} | **{r['score']:.0f}** | {r['px']:.2f} | "
                f"{r['chg']:+.2f}% | **可尾盘** | {r['why']} | {r['sell']} |"
            )
    elif meal_phase == "buy":
        lines.append("- **本轮尾盘狙击可买：没有。** 宁缺毋滥。")
    if meal_hold_picks:
        tag = "次日早盘应卖" if meal_phase == "sell" else "若已成交则持有待卖"
        lines.append(f"- **{tag}：** " + "、".join(
            f"{x.get('name')}({x.get('setup') or '-'} {x.get('px')})" for x in meal_hold_picks[:6]
        ))
    meal_watch = []
    for r in meal_watch_rows[:6]:
        meal_watch.append(f"{r['name']} {r.get('setup')} {r.get('call')} {r.get('score', 0):.0f}分")
    if meal_watch:
        tag = "14:30待确认" if meal_phase == "wait" else "在池未过闸"
        lines.append(f"- **{tag}：** " + "；".join(meal_watch))
    lines.append("")
    # ---- 2 胜率追踪：胜率-今日必买 / 胜率-尾盘狙击；对照表不是买点 ----
    rv = review or {}
    bt = rv.get("buy_track") or {}
    mt = rv.get("meal_track") or {}
    def _p(v, n=2):
        return "-" if v is None else f"{v:.{n}f}"
    def _pp(v):
        return "-" if v is None else f"{v:+.2f}%"
    def _sltp(r):
        sl, tp1, tp2 = r.get("sl"), r.get("tp1"), r.get("tp2")
        sl_s = _p(sl)
        if tp1 is None:
            return sl_s, "-"
        if tp2 is None:
            return sl_s, _p(tp1)
        return sl_s, f"{_p(tp1)}/{_p(tp2)}"
    def _track_block(title, note, empty, blob):
        lines.append(f"#### {title}")
        lines.append(note)
        if blob.get("n"):
            win_open = f"{blob['win_open']:.0f}%" if blob.get("win_open") is not None else "待次日"
            win_close = f"{blob['win_close']:.0f}%" if blob.get("win_close") is not None else "待次日收"
            lines.append(
                f"近{rv.get('days', 30)}日入选 {blob.get('n') or 0} 笔；"
                f"隔夜有数 {blob.get('n_open') or 0} 笔，隔夜胜率 {win_open}，隔夜均涨 {_pp(blob.get('avg_open'))}；"
                f"可成交有数 {blob.get('n_close') or 0} 笔，收盘胜率 {win_close}，均盈 {_pp(blob.get('avg_close'))}。"
                "样本少于20笔先别下结论。"
            )
            lines.append("| 入选日 | 名称 | 入选次数 | 入选价 | 成交价 | 口径 | 止损 | 止盈 | 次日开 | 隔夜 | 次日收 | 扣成本 | 结果 |")
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
            for r in blob.get("rows") or []:
                sl_s, tp_s = _sltp(r)
                nth = r.get("nth") or 1
                n_pick = r.get("n_pick") or 1
                times = f"第{nth}日/共{n_pick}日"
                when = (r.get("date") or "") + (f" {r['time']}" if r.get("time") else "")
                lines.append(
                    f"| {when} | {r.get('name') or r.get('code')} "
                    f"| {times} "
                    f"| {_p(r.get('px'))} | {_p(r.get('entry'))} | {r.get('how') or '-'} "
                    f"| {sl_s} | {tp_s} "
                    f"| {_p(r.get('nxt_open'))} | {_pp(r.get('open_pct'))} "
                    f"| {_p(r.get('nxt_close'))} | {_pp(r.get('net_close'))} | **{r.get('result') or '-'}** |"
                )
        else:
            lines.append(empty)

    def _call_mean(key):
        return {
            "可小仓": "当时买了",
            "可试仓": "当时买了（更轻）",
            "观察": "当时没买",
            "不追": "当时没追",
            "不买": "当时没买",
        }.get(key, "当时没买")

    def _disc_look(by_call):
        buy = next((r for r in by_call if r.get("key") == "可小仓"), None)
        watch = next((r for r in by_call if r.get("key") == "观察"), None)
        skip = next((r for r in by_call if r.get("key") == "不追"), None)
        bits = []
        if buy and watch:
            bits.append(
                f"可小仓次日胜率{buy['win']:.0f}%均收{buy['a1']:+.2f}%（{buy['n']}笔），"
                f"观察{watch['win']:.0f}%均收{watch['a1']:+.2f}%（{watch['n']}笔）。"
            )
            if buy["n"] < 8 or watch["n"] < 8:
                bits.append("样本太少，先不当真。")
            elif buy["win"] > watch["win"] + 5 and buy["a1"] > watch["a1"]:
                bits.append("可小仓打赢观察，闸有区分度。")
            else:
                bits.append("可小仓没有打赢观察，闸区分度不足；不要因此去买观察。")
        if skip and skip["n"] <= 5 and skip["win"] >= 80:
            bits.append("不追样本很小、次日均收高，多半是涨停/过热票惯性，不是该追的证据。")
        bits.append("3日/5日要等样本走过才有数。复盘只用来改阈值，不参与今天的判别。")
        return " ".join(bits)

    lines.append("## 2 胜率追踪")
    lines.append(
        "这一节只复盘，不改今天的闸。里面两块：**胜率-今日必买**（第0节）和 **胜率-尾盘狙击**（第1节尾盘狙击），不要混成一张总表。"
        "胜负怎么算：当时价买进 → 次日收盘卖掉（A股T+1），扣成本后赚钱=胜、亏钱=负。隔夜涨跌只是参考，不是胜负。"
        "「不买/不追的胜率」不是你没买也算赢，是**假如当时违闸买了**，次日收盘赚不赚钱。涨停惯性会让这格看起来很赚，所以不能拿来推翻今涨停不追。"
        "可小仓该打赢观察；打不赢去改阈值，不要去买观察。"
    )
    lines.append("### 胜率-今日必买")
    cost_txt = bt.get("cost_pct", rv.get("cost_pct", 0.1))
    _track_block(
        "当时买了的票（第0节可小仓）",
        "口径：每个交易日每只票只记**第一次可小仓**（入选日+入选价）。"
        "**同一天刷新、同一天反复出现在今日必买，都不加次数。** 尾盘狙击另表，不并进这里。"
        "盘中09:30–14:50入选，成交价=入选价；收盘后/盘前入选，成交价=次日开。"
        f"A股T+1，**胜负=次日收÷成交价，已扣成本{cost_txt}%**。"
        "隔夜=次日开相对入选价，只作隔夜参考。"
        "止损/止盈是入选当时的纪律价（第0节那套），不是事后改的；当时报告里有的会补上，没有才标-。"
        "入选次数=近窗该票可小仓**交易日数**，不是刷新次数；第2日/共3日=这是第2个交易日、一共3天进过今日必买。"
        "入选日后面的钟是第一次记下的时刻。次日收要等那天 15:00 收盘后那一轮快照才填，盘中和 15:00 前都是待收盘。",
        f"- 还没有可小仓留档（本次新增判别 {n_j} 条）。出现今日必买之后，这里会列出入选日和入选价。",
        bt,
    )
    lines.append("#### 当时没买的票（假如买了，不是推荐）")
    if rv.get("n_eval"):
        lines.append(
            f"口径：第0节全部判别留档 {rv['n_rec']} 条、已可评估 {rv['n_eval']} 条。"
            "不管当时闸说可小仓还是观察/不买/不追，都用**当时价 → 之后第1/3/5个交易日收盘**，"
            f"扣成本{rv.get('cost_pct')}%后赚钱就算这一格的「胜」。"
            "不含尾盘狙击。"
            "所以「不买 65%」不是不买也能赢，是：**假如当时违闸买了**，这批票里有 65% 次日收盘还是赚的。"
            "「不追」同理：过热没追，涨停惯性会让这格看起来很赚，不能拿来推翻今涨停不追。"
        )
        lines.append("| 当时闸怎么说 | 你当时买了没 | 样本 | 假如买了次日赚钱的比例 | 次日均收 | 3日均收 | 5日均收 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in rv.get("by_call") or []:
            a3 = f"{r['a3']:+.2f}%" if r["a3"] is not None else "-"
            a5 = f"{r['a5']:+.2f}%" if r["a5"] is not None else "-"
            ntxt = f"{r['n']}" + (" 少" if r["n"] < 8 else "")
            key = r.get("key") or "-"
            mark = f"**{key}**" if key == "可小仓" else key
            lines.append(
                f"| {mark} | {_call_mean(key)} | {ntxt} | {r['win']:.0f}% | {r['a1']:+.2f}% | {a3} | {a5} |"
            )
        by_kind = [r for r in (rv.get("by_kind") or []) if r["key"].startswith(("可小仓", "可试仓"))]
        if by_kind:
            lines.append("#### 按仓拆开（还是上面那批可小仓，不是另一套胜率）")
            lines.append(
                "把当时买了的票再按仓切开：可小仓/趋势、可小仓/游资、可小仓/早盘接力仓。"
                "跟上面「当时买了的票」是同一批，只是看哪一种仓更赚钱。"
            )
            lines.append("| 仓 | 样本 | 次日胜率 | 次日均收 | 3日均收 | 5日均收 |")
            lines.append("|---|---|---|---|---|---|")
            for r in by_kind[:8]:
                a3 = f"{r['a3']:+.2f}%" if r["a3"] is not None else "-"
                a5 = f"{r['a5']:+.2f}%" if r["a5"] is not None else "-"
                ntxt = f"{r['n']}" + (" 少" if r["n"] < 8 else "")
                k = r["key"]
                if "/" in k:
                    a, b = k.split("/", 1)
                    k = f"{a}/{display_kind(b)}"
                lines.append(
                    f"| {k} | {ntxt} | {r['win']:.0f}% | {r['a1']:+.2f}% | {a3} | {a5} |"
                )
        lines.append("- 看法：" + _disc_look(rv.get("by_call") or []))
    else:
        lines.append(f"- 对照表要等隔一个交易日才有可评估样本（本次新增 {n_j} 条）。")
    lines.append("")

    lines.append("### 胜率-尾盘狙击")
    lines.append(
        "第1节「尾盘狙击」独立仓的成绩，不进今日必买，也不跟第0节闸混在一张表里。"
        "第一次可尾盘记一笔；隔夜看次日开，收盘胜率看到次日收只作对照。"
    )
    _track_block(
        "当时尾盘买了的票（可尾盘）",
        "口径：每个交易日每只票只记**第一次可尾盘**。同一天刷新不加次数。14:30后入选价=当时尾盘价，不改成次日开。"
        "隔夜胜率=次日开÷入选价，这是早盘兑现的参考。"
        "收盘胜率是拿到次日收，比策略10:00清完更晚，只作对照。"
        "不进今日必买。入选次数=近窗该票可尾盘**交易日数**，第2日/共3日=第2个交易日、一共3天。",
        "- 还没有可尾盘留档。第1节筛出可尾盘后，这里会列出入选日和尾盘价。"
        + (f"（本次新增 {n_jm} 条）" if n_jm else ""),
        mt,
    )
    lines.append("")

    lines.append("## 3 板块资金")
    flow_src = "盘中实时" if cn_flow_live() else "休市/竞价=昨收最新（live 优先，连不上再用 delay）"
    lines.append(
        f"口径：东财行业主力净流入（估算），另按自选每只票的东财行业/对口概念点名细桶。"
        f"{flow_src}。"
        "流入/流出只定主线热度，不单独开仓。"
        "板块热度与主线可做/回避是同一笔当日主力，不是两道条件；全市场涨停情绪另算。"
    )
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
    lines.append("## 4 集合竞价")
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
    lines.append("买点=今日必买。分=均线健康。买点分=九列。值分=闸+主线热+盘面(趋势买点分/游资7a)×0.28+均线分×0.18+竞价±3。买点分不重复加。竞价列只展示判断，不单独改买点。表一买点与明细标的相同，只保留明细（含买点表多出来的竞价/值序/为什么）。")
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
        miss_rows.append((s["name"] in spotlight, -(yz.get("score") or 0), s["name"], s, q, yz, call, why))
    miss_rows.sort()
    for _, _, _, s, q, yz, call, why in miss_rows[:12]:
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
    lines.append(f"### 分池（趋势{len(left_trend)}+游资{len(left_youzi)}，共{len(left_merged)}只）")
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
    lines.append("- 因子：昨板质量、题材、主线、市值、换手、竞价3-7%站住开盘、连板高度、昨板赚钱效应。**今首板不追**；昨一字不打；二进三只盯。")
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
    lines.append("- 口径：板块冷/热看东财行业主力净流入，不是看涨幅热门。回避=该线自己资金净出。亨通跟CPO，和光模块放在光通信；东财通信线缆流出不把CPO打冷。元件/PCB流出不连坐光通信、半导体、液冷。三花跟汽车热管理。中材=玻纤。太极=半导体封测。每只票跟自己的东财行业或对口概念独立桶（紫金=工业金属，不跟有色金属连坐；恒瑞=创新药，不跟医药生物连坐），不写死板块代码。")
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
    lines.append(f"**今日必买：{buy_line}**")
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
        lines.append(f"| 早盘接力仓 | {name} | {px:.2f} | {chg:+.2f}% | 打板{sc:.0f} | {line}/{st} | {how} | {fit} | **可小仓** | {sl} | {tp} |")
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
    lines.append("总判规则：趋势=表一可小仓且主线不是回避；早盘接力仓=今首板不追，昨首板一进二/弱转强/龙回头才可能可小仓；游资=7a过门槛（资金缺72/退潮75，平时65）、未涨停见顶、主线不是回避；ETF同主线回避也降观察；左侧=轻仓试。最适合买：趋势 > 早盘接力仓 > 游资 > ETF > 左侧。今涨停不追。昨跌停骗炮才不买，弱转强放宽为低开翻红站住均价。尾盘隔夜仓见第1节，不进本表。")
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
        lines.append("- 游资仓：情绪退潮，门槛抬到7a≥75且只做低位转强，不一律空仓；打板仍空仓")
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
    lines.append("## 15 买点钟（游资/趋势/早盘接力仓/尾盘狙击各有窗口）")
    lines.append(
        f"**现在：{clk['slot']}　{clk['name']}。{clk['action']}**"
    )
    now_act = style_now_action(now)
    lines.append(
        f"- 本窗口动作：游资 **{clk['youzi']}** · 趋势 **{clk['trend']}** · 早盘接力仓 **{clk['daban']}** · 尾盘狙击 **{clk['meal']}**"
    )
    lines.append(
        f"- 现在买卖：游资买 {now_act['youzi_buy']} / 卖旧仓 {now_act['youzi_sell']}；"
        f"趋势买 {now_act['trend_buy']} / 卖 {now_act['trend_sell']}；"
        f"早盘接力仓买 {now_act['daban_buy']} / 卖旧仓 {now_act['daban_sell']}；"
        f"尾盘狙击买 {now_act.get('meal_buy') or clk['meal']} / 卖 {now_act.get('meal_sell') or '-'}"
    )
    lines.append("| 仓 | 买（胜率相对高） | 这时不买 | 卖（胜率相对高） | 拿多久 |")
    lines.append("|---|---|---|---|---|")
    for b in style_books():
        lines.append(
            f"| **{b['kind']}** | {b['buy']} | {b['skip']} | {b['sell']} | {b['hold']} |"
        )
    lines.append("| 时段 | 叫什么 | 主流怎么用 |")
    lines.append("|---|---|---|")
    for a, b, c in clk["table"]:
        if clk["slot"] == a:
            lines.append(f"| **{a}** | **{b}** | **{c}** |")
        else:
            lines.append(f"| {a} | {b} | {c} |")
    lines.append(
        "- 游资真开仓：只做 **09:35–10:15**，10:30后再刷只确认还在不新开。"
        "散户卖在 **次日 09:31–09:50**，最晚 10:00。这是市场里短打胜率最高的映射，不是当天下午卖今天买的票。"
    )
    lines.append(
        "- 趋势真开仓：**10:00–10:30 回踩** 和 **13:00–13:30 午后确认**，不抢开盘三分钟、不追 14:00 后新高。"
        "卖按结构：冲高 09:45–10:15 减、破计划位立刻走、主线走弱次日早盘清。"
    )
    lines.append(
        "- 早盘接力仓真开仓：只做昨板接力 **09:32–09:50**（不炸）或弱转强 **09:35–10:00**。今首板和尾盘偷鸡不做。"
        "次日不封/开板 **09:30–09:45 走**。未晋级不隔第二夜。"
    )
    lines.append(
        "- 尾盘狙击真开仓：独立仓，只做 **14:40–14:55**（14:30–14:40先看盘，不抢假拉；14:57收盘集合不追）。"
        "必须过第1节可尾盘：隔夜强势或尾盘二板；均价下/近10日无涨停/非近3日主线不做；不做尾盘首板偷鸡。"
        "次日 **09:31–09:50 冲高卖**（大约+3%走），低开/开板 **09:30–09:35 先走**（大约-2%），最晚 **10:00 清完**。"
        "竞价可以挂卖。一字涨停可暂留，其余不隔第二夜。不进第0节可以买。"
    )
    lines.append("- 散户T+1：" + clk["retail"])
    lines.append("- 只标钟，不改第0节闸，也不把尾盘狙击写进今日必买：过闸才能买，没过闸这个钟不能把观察票洗成今日必买。")
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
        "avoid_detail": [
            {
                "line": x,
                "amt": (heat_map.get(x) or {}).get("amt"),
                "chg": (heat_map.get(x) or {}).get("chg"),
            }
            for x in desk_avoid[:10]
        ],
        "ovn_desk": ovn_desk,
        "can_small": can_small,
        "watch": watch,
        "small_on_line": small_on_line,
        "buy_names": buy_bits,
        "buy_line": buy_line,
        "buy_books": {
            "trend": [x[0] for x in trend_ok],
            "youzi": [x[0] for x in youzi_ok],
            "daban": [x[0] for x in daban_ok],
            "etf": [x[0] for x in etf_ok],
        },
        "top5": [{"name": x["name"], "role": x["role"], "score": round(x["score"], 1), "heat": x["heat"],
                  "sl": (exits_by_name.get(x["name"]) or {}).get("sl"),
                  "tp": (lambda e: f"{e['tp1']}/{e['tp2']}" if e else None)(exits_by_name.get(x["name"]))} for x in top5],
        "alt_pool": [{"name": x["name"], "call": x["call"], "score": round(x["score"], 1),
                      "sl": (exits_by_name.get(x["name"]) or {}).get("sl"),
                      "tp": (lambda e: f"{e['tp1']}/{e['tp2']}" if e else None)(exits_by_name.get(x["name"]))} for x in alt_pool],
        "daban": [{"name": x[0], "score": x[3], "setup": (daban_by_code.get(x[7]) or {}).get("setup")} for x in daban_ok],
        "overnight_meal": {
            "phase": meal_phase,
            "ok": meal_ok[:5],
            "hold": meal_hold_picks[:5],
            "watch": meal_watch_rows[:5],
            "note": "独立仓，不进第0节今日必买，不跟游资/趋势/打板混闸",
            "hot_lines": sorted(hot_lines)[:12],
        },
        "buy_clock": {
            "slot": clk["slot"],
            "name": clk["name"],
            "action": clk["action"],
            "youzi": clk["youzi"],
            "trend": clk["trend"],
            "daban": clk["daban"],
            "meal": clk["meal"],
            "now": style_now_action(now),
        },
        "left_try": left_names,
        "buy_track": {
            "n": (bt or {}).get("n") or 0,
            "n_open": (bt or {}).get("n_open") or 0,
            "win_open": (bt or {}).get("win_open"),
            "avg_open": (bt or {}).get("avg_open"),
            "n_close": (bt or {}).get("n_close") or 0,
            "win_close": (bt or {}).get("win_close"),
            "avg_close": (bt or {}).get("avg_close"),
        },
        "meal_track": {
            "n": (mt or {}).get("n") or 0,
            "n_open": (mt or {}).get("n_open") or 0,
            "win_open": (mt or {}).get("win_open"),
            "avg_open": (mt or {}).get("avg_open"),
            "n_close": (mt or {}).get("n_close") or 0,
            "win_close": (mt or {}).get("win_close"),
            "avg_close": (mt or {}).get("avg_close"),
        },
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
        "data_health": {"missing_quote": miss_q, "fetch_fail": FETCH_FAIL, "stale": FLOW_STALE},
        "idx_tape": idx_tape,
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
    if header in ("今涨", "涨跌", "当日", "开盘涨幅", "次日盈亏", "隔夜", "扣成本"):
        if t.startswith("+"):
            return "up"
        if t.startswith("-"):
            return "dn"
    if header in ("状态", "判断", "怎么做", "操作分", "买点", "角色", "竞价判断", "竞价", "价量同向", "流向", "结果"):
        if any(k in t for k in ("可小仓", "可试仓", "可做", "可买", "抢筹强", "胜")):
            return "ok"
        if any(k in t for k in ("不买", "不追", "回避", "剔除", "骗炮", "见顶", "板块冷", "砸盘弱", "负")):
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
h2#snews { border:0; margin:2px 0 14px; color:#c5c2b6; font-size:13px; padding-top:0; }
h2#s0 { margin:36px 0 10px; color:#f2f1ec; font-size:13px; }
h2#swin { color:#c5c2b6; font-size:13px; }
.sec0 h2#s0 { font-size:clamp(26px,4.8vw,36px); font-weight:800; color:#fff; letter-spacing:0;
  border-top:0; padding-top:8px; margin-top:28px; }
.sec0 .hero { padding:22px 24px; margin:0 0 18px; border-color:rgba(242,241,236,.22); }
.sec0 .hero p { font-size:clamp(18px,3.2vw,24px); font-weight:800; line-height:1.45; color:#fff; }
.sec0 .hero p:first-child { font-size:clamp(20px,3.8vw,28px); }
.sec0 table { font-size:16px; }
.sec0 th { font-size:13px; }
.sec0 td, .sec0 th { padding:14px 16px; }
.sec0 td:first-child { font-weight:800; color:#fff; }
.sec0 h3 { font-size:clamp(20px,3vw,24px); font-weight:800; }
.sec0 p, .sec0 li { font-size:16px; color:#e8e6de; }
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
  .sec0 .hero p { font-size:18px; }
  .sec0 .hero p:first-child { font-size:20px; }
  .sec0 table { font-size:14px; }
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
        '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Noto+Sans+SC:wght@400;500;700;800&display=swap" rel="stylesheet">',
        f"<style>{css}</style></head><body>",
        "<nav class=toc>",
        "<a href='#snews'>最新资讯</a>",
        "<a href='#s0'>0 今日必买</a>",
        "<a href='#s1'>1 尾盘狙击</a>",
        "<a href='#s2'>2 胜率追踪</a>",
        "<a href='#s3'>3 板块资金</a>",
        "<a href='#s4'>4 集合竞价</a>",
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
        "<a href='#s15'>15 买点钟</a>",
        "</nav><div class=page>",
    ]
    lines = md.replace("\r\n", "\n").split("\n")
    i = 0
    hero_open = False
    pending_hero = False
    sec0_open = False
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
            if sec0_open:
                parts.append("</section>")
                sec0_open = False
            title_txt = line[3:].strip()
            m = re.match(r"^(\d+[a-z]?)", title_txt)
            if m:
                hid = f"s{m.group(1)}"
            elif title_txt.startswith("最新资讯"):
                hid = "snews"
            else:
                hid = ""
            if hid == "s0":
                parts.append("<section class=sec0>")
                sec0_open = True
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
            wrap_headers = {"7因子", "资金/暗盘代理", "怎么做", "止跌确认", "左侧确认", "仓位", "为什么", "依据", "主线热度", "主线", "位置", "斐波那契", "未进可买的原因", "说明", "领涨", "板块", "战法", "止损", "止盈", "次日怎么卖", "主流怎么用", "买（胜率相对高）", "这时不买", "卖（胜率相对高）", "拿多久", "隔夜依据", "映射自选板块", "A股资金", "隔夜主题"}
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
    if sec0_open:
        parts.append("</section>")
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
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        tz = datetime.timezone(datetime.timedelta(hours=8))

        def t(h, m, wd=0):
            d = datetime.datetime(2026, 9, 21, h, m, tzinfo=tz)
            # 2026-09-21 is Monday
            return d.replace() if wd == 0 else d + datetime.timedelta(days=wd)

        n = style_now_action(t(9, 50))
        assert n["youzi_buy"] == "主买点" and "主卖点" in n["youzi_sell"], n
        n = style_now_action(t(10, 20))
        assert n["trend_buy"].startswith("主买点") and "最晚清完" in n["youzi_sell"], n
        n = style_now_action(t(13, 10))
        assert "第二买点" in n["trend_buy"], n
        books = style_books()
        assert [b["kind"] for b in books] == ["游资", "趋势", "早盘接力仓", "尾盘狙击"]
        n = style_now_action(t(14, 45))
        assert "主买" in n["meal_buy"], n
        n = style_now_action(t(14, 35))
        assert "看盘" in n["meal_buy"], n
        n = style_now_action(t(9, 40))
        assert "主卖" in n["meal_sell"], n
        assert books[3]["buy"].startswith("14:40"), books[3]
        c = buy_clock(t(9, 50))
        assert c["youzi"] == "主买点" and c["name"] == "游资黄金买点", c
        c = buy_clock(t(10, 20))
        assert c["name"] == "回踩确认" and "第二买点" in c["youzi"], c
        c = buy_clock(t(14, 10))
        assert c["name"] == "游资最后窗口", c
        c = buy_clock(t(14, 35))
        assert c["name"] == "尾盘观察", c
        c = buy_clock(t(14, 45))
        assert c["youzi"] == "不新开" and c["meal"].startswith("真开仓"), c
        assert any(x[1] == "尾盘狙击主买" for x in c["table"]), c["table"]
        c = buy_clock(t(11, 40))
        assert c["name"] == "午休", c
        src = open(__file__, encoding="utf-8").read()
        gen = src.split('if __name__')[0]
        assert gen.find("## 15 买点钟") > gen.find("## 14 买点明细")
        assert gen.find("## 2 胜率追踪") > gen.find("## 1 尾盘狙击")
        assert gen.find("### 胜率-今日必买") > gen.find("## 2 胜率追踪")
        assert gen.find("### 胜率-尾盘狙击") > gen.find("### 胜率-今日必买")
        assert "入选次数" in gen
        assert "第{nth}日/共{n_pick}日" in gen
        assert "同一天刷新" in gen
        assert gen.find("## 3 板块资金") > gen.find("### 胜率-尾盘狙击")
        assert gen.find("## 5 个股一览") > gen.find("## 4 集合竞价")
        assert gen.find("## 6 趋势复核") > gen.find("## 5 个股一览")
        assert "### 0b 早盘接力仓" in gen
        assert "1 尾盘打板" not in gen
        assert "胜率追踪-尾盘打板" not in gen
        assert "胜率追踪-尾盘隔夜仓" not in gen
        assert display_kind("打板") == "早盘接力仓"
        assert display_kind("尾盘隔夜仓") == "尾盘狙击"
        assert display_kind("午夜关注") == "尾盘狙击"
        assert display_kind("趋势") == "趋势"
        assert "### 0d 可以买跟踪" not in gen
        assert "### 0a 买点钟" not in gen
        assert "### 0e 最新资讯" not in gen
        assert gen.find("lines.extend(overnight_news_lines") < gen.find('lines.append("## 0 今日必买")')
        assert "## 0 能不能买" not in gen
        assert "仓怎么分：" in gen and "游资≠打板" in gen
        assert "section class=sec0" in gen
        assert ".sec0 h2#s0" in gen
        html0 = render_report_html(
            "# 今日自选 2026-09-22 12:00 北京\n\n"
            "## 0 今日必买\n**今日必买：无**\n"
            "**仓怎么分：游资≠打板。** 游资是7a短线仓。\n"
            "## 1 尾盘狙击\n正文\n"
        )
        assert "<section class=sec0>" in html0
        assert html0.find("<section class=sec0>") < html0.find("id='s0'")
        assert html0.find("</section>") < html0.find("id='s1'")
        assert "游资≠打板" in html0
        assert _em_diff({"data": {"diff": [{"f12": "1"}]}})[0]["f12"] == "1"
        assert _em_diff({"data": {"diff": {"0": {"f12": "2"}}}})[0]["f12"] == "2"
        assert _em_diff({"data": {"diff": None}}) == []
        assert overnight_meal_phase(t(14, 45)) == "buy"
        assert overnight_meal_phase(t(9, 40)) == "sell"
        assert overnight_meal_phase(t(12, 0)) == "wait"
        assert youzi_late(t(14, 45)) is True
        assert youzi_late(t(10, 0)) is False
        assert _day_done(t(14, 59), "2026-09-21") is False
        assert _day_done(t(15, 0), "2026-09-21") is True
        assert _day_done(t(15, 1), "2026-09-21") is True
        assert _day_done(t(10, 0), "2026-09-18") is True
        meal_recs = [{
            "date": "2026-09-18", "time": "14:45", "code": "000001", "name": "平安银行",
            "call": "可尾盘", "px": 10.0,
        }]
        meal_hist = {"000001": [
            ["2026-09-18", 10.0, 10.2, 9.8, 10.1, 1],
            ["2026-09-21", 10.4, 10.8, 10.2, 10.6, 1],
        ]}
        mt = _buy_track(meal_recs, meal_hist, t(10, 0), 0.1, call="可尾盘")
        assert mt["n"] == 1 and mt["rows"][0]["how"] == "盘中价", mt
        assert _buy_track(meal_recs, meal_hist, t(10, 0), 0.1)["n"] == 0
        s = {"code": "002475", "name": "立讯精密", "asset": "stock"}
        q = {
            "px": 42.0, "high": 42.3, "vwap": 41.2, "chg": 4.2, "turnover": 7.2,
            "vol_ratio": 1.6, "prev": 40.3, "open": 40.8, "mcap": 120, "low": 40.6,
        }
        meal = overnight_meal_plan(
            s, q, {}, None, "可做", "消费电子", {"phase": "修复"}, t(14, 45), {}, {}, [],
        )
        assert meal["setup"] == "隔夜强势" and meal["call"] == "可尾盘" and meal["score"] >= 70, meal
        meal2 = overnight_meal_plan(
            s, q, {}, None, "可做", "消费电子", {"phase": "修复"}, t(10, 30), {}, {}, [],
        )
        assert meal2["call"] != "可尾盘", meal2
        first = {"code": "000001", "name": "平安银行"}
        qz = dict(q)
        qz.update({"chg": 9.95, "px": 11.0, "high": 11.0, "prev": 10.01, "open": 11.0, "turnover": 12.0, "vwap": 10.6})
        hist = [
            ["20260917", 10, 10.2, 9.8, 10, 1],
            ["20260918", 10, 11.0, 10, 10.99, 1],
        ]
        zt_y = {"000001": {"lbc": 1, "zbc": 0, "hard": True}}
        meal3 = overnight_meal_plan(
            first, qz, {}, None, "可做", "银行", {"phase": "修复"}, t(14, 45), zt_y, {"000001": {"lbc": 2, "zbc": 0, "hard": True}}, hist,
        )
        assert meal3["setup"] == "尾盘二板" and meal3["score"] >= 70, meal3
        q_big = dict(q)
        q_big["mcap"] = 3056
        meal_big = overnight_meal_plan(
            {"code": "600276", "name": "恒瑞医药", "asset": "stock"},
            q_big, {}, None, "可做", "创新药", {"phase": "修复"}, t(14, 45), {}, {}, [],
        )
        assert meal_big["call"] == "不买" and meal_big["setup"] == "大盘不做", meal_big
        q_nom = dict(q_big)
        q_nom["mcap"] = 0
        meal_nom = overnight_meal_plan(
            {"code": "600276", "name": "恒瑞医药", "asset": "stock"},
            q_nom, {}, None, "可做", "创新药", {"phase": "修复"}, t(14, 45), {}, {}, [],
        )
        assert meal_nom["call"] == "不买" and meal_nom["setup"] == "市值未知", meal_nom
        assert line_status_of({"board": "创新药"}, ["光通信"], ["医药"]) == ("中性", "创新药")
        assert line_status_of({"board": "创新药"}, [], ["创新药"]) == ("回避", "创新药")
        assert line_status_of({"board": "医药游资"}, [], ["CRO"]) == ("中性", "游资医药")
        assert line_status_of({"board": "医药游资"}, [], ["医药"]) == ("中性", "游资医药")
        assert line_status_of({"board": "医药"}, [], ["医药"]) == ("回避", "医药")
        hp, tag = heat_pts_of("中性", "创新药", {"医药": {"amt": -7.4}})
        assert hp == 0 and "主力" not in tag, (hp, tag)
        hp2, tag2 = heat_pts_of("回避", "创新药", {"创新药": {"amt": -10}})
        assert "主力-10" in tag2, tag2
        inn2, out2 = flow_sets(["创新药 0.75% 主力-10.5亿"], ["CRO 1.57% 主力-3.6亿", "医药生物 0.8% 主力-20.0亿"])
        assert "创新药" in inn2 and "CRO" in out2 and "医药" in out2, (inn2, out2)
        assert _pick_primary("有色", "有色", "工业金属", ["黄金概念", "稀缺资源"]) == "工业金属"
        assert _pick_primary("有色", "有色", "小金属", ["小金属概念"]) == "小金属"
        assert _pick_primary("创新药", "创新药", "化学制药", ["创新药", "减肥药"]) == "创新药"
        assert _pick_primary("热管理", "热管理", "家电零部件Ⅱ", ["汽车热管理", "液冷服务器"]) == "汽车热管理"
        assert _pick_primary("医药游资", "游资医药", "医疗服务", ["CRO", "创新药"]) == "医疗服务"
        old_keep, old_hy = set(FLOW_KEEP), dict(STOCK_HY)
        try:
            FLOW_KEEP.clear()
            FLOW_KEEP.add("工业金属")
            FLOW_KEEP.add("小金属")
            inn3, out3 = flow_sets(
                ["有色金属 0.5% 主力+7.8亿"],
                ["工业金属 0.6% 主力-3.0亿", "小金属 0.0% 主力+3.0亿"],
            )
            assert "有色" in inn3 and "工业金属" in out3 and "小金属" in out3, (inn3, out3)
            assert "有色" not in out3, out3
            STOCK_HY.clear()
            STOCK_HY["601899"] = {"primary": "工业金属", "hy": "工业金属", "concepts": ["黄金概念"]}
            STOCK_HY["000657"] = {"primary": "小金属", "hy": "小金属", "concepts": ["小金属概念"]}
            STOCK_HY["301520"] = {"primary": "医疗服务", "hy": "医疗服务", "concepts": ["CRO", "创新药"]}
            assert line_status_of({"code": "601899", "board": "有色"}, [], ["有色"]) == ("中性", "工业金属")
            assert line_status_of({"code": "601899", "board": "有色"}, [], ["工业金属"]) == ("回避", "工业金属")
            assert line_status_of({"code": "000657", "board": "有色"}, ["工业金属"], ["小金属"]) == ("回避", "小金属")
            assert line_status_of({"code": "301520", "board": "医药游资"}, [], ["医药"]) == ("中性", "医疗服务")
            assert line_status_of({"code": "301520", "board": "医药游资"}, [], ["医疗服务"]) == ("回避", "医疗服务")
        finally:
            FLOW_KEEP.clear()
            FLOW_KEEP.update(old_keep)
            STOCK_HY.clear()
            STOCK_HY.update(old_hy)
        sample_md = (
            "| 趋势 | 三花智控 | 35.94 | +1.78% | **可小仓** | 热管理/中性 | "
            "站回均价且未破关键低、涨幅未过热 | 34.17(-4.9%) | 37.83(+5.3%) / 38.84(+8.1%) |"
        )
        assert _sltp_from_md(sample_md, "三花智控") == (34.17, 37.83, 38.84)
        sl_recs = [{
            "date": "2026-09-22", "time": "09:44", "code": "600276", "name": "恒瑞医药",
            "call": "可小仓", "px": 45.91, "sl": 44.19, "tp1": 48.98, "tp2": 50.64,
        }]
        bt = _buy_track(sl_recs, {}, t(10, 0), 0.1)
        assert bt["rows"][0]["sl"] == 44.19 and bt["rows"][0]["tp1"] == 48.98, bt["rows"][0]
        n_recs = [
            {"date": "2026-09-18", "time": "18:42", "code": "300502", "name": "新易盛", "call": "可小仓", "px": 445.0},
            {"date": "2026-09-21", "time": "09:40", "code": "300502", "name": "新易盛", "call": "可小仓", "px": 463.36},
            {"date": "2026-09-21", "time": "09:39", "code": "002050", "name": "三花智控", "call": "可小仓", "px": 35.94},
        ]
        nt = _buy_track(n_recs, {}, t(10, 0), 0.1)
        by = {(r["code"], r["date"]): r for r in nt["rows"]}
        assert by[("300502", "2026-09-18")]["n_pick"] == 2 and by[("300502", "2026-09-18")]["nth"] == 1, by
        assert by[("300502", "2026-09-21")]["nth"] == 2 and by[("300502", "2026-09-21")]["n_pick"] == 2, by
        assert by[("002050", "2026-09-21")]["n_pick"] == 1 and by[("002050", "2026-09-21")]["nth"] == 1, by
        same_day = n_recs + [
            {"date": "2026-09-22", "time": "10:01", "code": "002354", "name": "天娱数科", "call": "可小仓", "px": 8.06},
            {"date": "2026-09-22", "time": "13:16", "code": "002354", "name": "天娱数科", "call": "可小仓", "px": 8.02},
            {"date": "2026-09-22", "time": "10:45", "code": "300413", "name": "芒果超媒", "call": "可小仓", "px": 19.44},
            {"date": "2026-09-21", "time": "09:50", "code": "300413", "name": "芒果超媒", "call": "可小仓", "px": 18.49},
        ]
        sd = _buy_track(same_day, {}, t(13, 20), 0.1)
        ty = [r for r in sd["rows"] if r["code"] == "002354"]
        mg = [r for r in sd["rows"] if r["code"] == "300413"]
        assert len(ty) == 1 and ty[0]["n_pick"] == 1 and ty[0]["time"] == "10:01", ty
        assert len(mg) == 2 and mg[0]["n_pick"] == 2, mg
        assert "第1日/共1日" in (
            f"第{ty[0]['nth']}日/共{ty[0]['n_pick']}日"
        )
        sh_recs = [{
            "date": "2026-09-21", "time": "09:39", "code": "002050", "name": "三花智控",
            "call": "可小仓", "px": 35.94,
        }]
        sh_bt = _buy_track(sh_recs, {}, t(10, 0), 0.1)
        if os.path.isfile(os.path.join(ROOT, "reports", "20260921_0939.md")):
            assert sh_bt["rows"][0]["sl"] == 34.17 and sh_bt["rows"][0]["tp1"] == 37.83, sh_bt["rows"][0]
        q_mid = dict(q)
        q_mid["mcap"] = 399
        meal_mid = overnight_meal_plan(
            s, q_mid, {}, None, "可做", "消费电子", {"phase": "修复"}, t(14, 45), {}, {}, [],
        )
        assert meal_mid["setup"] == "隔夜强势" and meal_mid["call"] == "可尾盘", meal_mid
        q_vw = dict(q)
        q_vw["px"] = 40.5
        meal_vw = overnight_meal_plan(
            s, q_vw, {}, None, "可做", "消费电子", {"phase": "修复"}, t(14, 45), {}, {}, [],
        )
        assert meal_vw["call"] == "不买" and meal_vw["setup"] == "均价下不做", meal_vw
        meal_off = overnight_meal_plan(
            s, q, {}, None, "中性", "传媒", {"phase": "修复"}, t(14, 45), {}, {}, [], ["医药"],
        )
        assert meal_off["call"] == "不买" and meal_off["setup"] == "非近3日主线", meal_off
        meal_hot = overnight_meal_plan(
            s, q, {}, None, "中性", "传媒", {"phase": "修复"}, t(14, 45), {}, {}, [], ["传媒"],
        )
        assert meal_hot["setup"] == "隔夜强势" and meal_hot["call"] == "可尾盘", meal_hot
        cold = []
        px = 10.0
        for i in range(8):
            day = f"202608{i+1:02d}"
            cold.append([day, px, px + 0.2, px - 0.2, px + 0.1, 1])
            px += 0.1
        meal_gene = overnight_meal_plan(
            s, q, {}, None, "可做", "消费电子", {"phase": "修复"}, t(14, 45), {}, {}, cold,
        )
        assert meal_gene["call"] == "不买" and meal_gene["setup"] == "无涨停基因", meal_gene
        print("SELFTEST_OK", meal["score"], meal3["setup"], meal3["score"], c["slot"])
        sys.exit(0)
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

