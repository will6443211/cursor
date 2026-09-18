#!/usr/bin/env python3
"""Watchlist add/list/remove API for the 自选管理 page."""
import json, os, re, fcntl, datetime, secrets, hmac, threading, subprocess, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.abspath(__file__))
WL_PATH = os.path.join(ROOT, "watchlist.json")
SECRET_PATH = os.path.join(ROOT, ".watch_secret")
LOG_DIR = os.path.join(ROOT, "logs")
SNAP_LOCK = os.path.join(LOG_DIR, "run.lock")
SNAP_LOG = os.path.join(LOG_DIR, "manual.log")
SNAP_STATE = os.path.join(LOG_DIR, "run_job.json")
TOKEN_TTL = 12 * 3600

import run_snapshot as rs

_lock = threading.Lock()
_tokens = {}  # token -> expire ts
_job_lock = threading.Lock()
_job = {"running": False, "pid": None, "started": None, "error": None}


def load_password():
    if os.path.isfile(SECRET_PATH):
        return open(SECRET_PATH, encoding="utf-8").read().strip()
    return os.environ.get("WATCH_PASSWORD") or ""


def password_ok(given):
    expected = load_password()
    if not expected or given is None:
        return False
    return hmac.compare_digest(str(given).encode("utf-8"), expected.encode("utf-8"))


def new_token():
    tok = secrets.token_urlsafe(32)
    exp = datetime.datetime.now(datetime.timezone.utc).timestamp() + TOKEN_TTL
    with _lock:
        _tokens[tok] = exp
    return tok


def token_ok(tok):
    if not tok:
        return False
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    with _lock:
        exp = _tokens.get(tok)
        if not exp:
            return False
        if exp < now:
            _tokens.pop(tok, None)
            return False
        return True


def _job_alive():
    pid = _job.get("pid")
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def snapshot_status():
    with _job_lock:
        running = bool(_job.get("running") and _job_alive())
        if _job.get("running") and not running:
            _job["running"] = False
            _job["pid"] = None
        st = dict(_job)
    latest = {}
    jp = os.path.join(ROOT, "reports", "latest.json")
    if os.path.isfile(jp):
        try:
            latest = json.load(open(jp, encoding="utf-8"))
        except Exception:
            latest = {}
    log_tail = ""
    if os.path.isfile(SNAP_LOG):
        try:
            lines = open(SNAP_LOG, encoding="utf-8", errors="replace").read().splitlines()
            log_tail = "\n".join(lines[-8:])
        except Exception:
            log_tail = ""
    return {
        "ok": True,
        "running": running,
        "started": st.get("started"),
        "error": st.get("error"),
        "latest_time": latest.get("time"),
        "buy_today": latest.get("buy_today"),
        "buy_names": latest.get("buy_names") or [],
        "top5": latest.get("top5") or [],
        "log": log_tail,
    }


def start_snapshot():
    os.makedirs(LOG_DIR, exist_ok=True)
    with _job_lock:
        if _job.get("running") and _job_alive():
            return {"ok": True, "started": False, "running": True, "error": "已有快照在跑，请稍候"}
        logf = open(SNAP_LOG, "ab")
        env = os.environ.copy()
        env["TZ"] = "Asia/Shanghai"
        p = subprocess.Popen(
            ["/usr/bin/flock", "-n", SNAP_LOCK, "/usr/bin/python3", os.path.join(ROOT, "run_snapshot.py")],
            cwd=ROOT,
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        _job.update({
            "running": True, "pid": p.pid,
            "started": datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S"),
            "error": None,
        })

        def _wait():
            rc = p.wait()
            with _job_lock:
                _job["running"] = False
                _job["pid"] = None
                if rc != 0:
                    _job["error"] = "未能开跑（可能已有定时任务在跑）" if rc == 1 else f"快照退出码 {rc}"
            try:
                logf.close()
            except Exception:
                pass

        threading.Thread(target=_wait, daemon=True).start()
    return {"ok": True, "started": True, "running": True, "error": None}


def infer_market(code):
    if code.startswith(("6", "5", "9", "688")):
        return "sh"
    return "sz"


def resolve_code(query):
    """6位代码或中文名（东华软件）→ A股/ETF代码。先自选，再东财联想。"""
    q = (query or "").strip()
    if not q:
        return None, "请输入6位代码或股票名称"
    m6 = re.search(r"(\d{6})", q)
    if re.fullmatch(r"\d{6}", q) or (m6 and re.fullmatch(r"[\dA-Za-z.\s]+", q)):
        if m6:
            return m6.group(1), None
    try:
        wl = read_wl()
    except Exception:
        wl = {}
    names = []
    for bucket in ("stocks", "etfs"):
        for x in wl.get(bucket) or []:
            names.append(x)
    exact = [x for x in names if (x.get("name") or "") == q]
    part = [x for x in names if q in (x.get("name") or "")]
    hit = (exact or part)
    if hit:
        return hit[0].get("code"), None
    try:
        from urllib.parse import quote
        url = (
            "https://searchapi.eastmoney.com/api/suggest/get?input="
            + quote(q)
            + "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=8"
        )
        data = rs.http(url, timeout=8) or {}
        rows = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
        stocks = []
        for row in rows:
            code = str(row.get("Code") or row.get("UnifiedCode") or "")
            name = row.get("Name") or ""
            cls = row.get("Classify") or ""
            st = row.get("SecurityTypeName") or ""
            if len(code) != 6 or not code.isdigit():
                continue
            if any(k in (cls + st) for k in ("指数", "Index", "债券", "回购")):
                continue
            stocks.append((code, name, cls, st))
        if stocks:
            exact = [x for x in stocks if x[1] == q]
            a = [x for x in (exact or stocks) if "A" in (x[2] + x[3]) or x[2] == "AStock"]
            pick = (exact or a or stocks)[0]
            return pick[0], None
    except Exception:
        pass
    try:
        from urllib.parse import quote
        raw = rs.http(
            "https://smartbox.gtimg.cn/s3/?v=2&q=" + quote(q) + "&t=all",
            gbk=True, timeout=8,
        )
        # v_hint="sz~002065~\u4e1c\u534e\u8f6f\u4ef6~dhrj~GP-A"
        text = raw if isinstance(raw, str) else str(raw)
        mm = re.search(r"(?:sh|sz|SH|SZ)~(\d{6})~([^~]+)~", text)
        if mm:
            return mm.group(1), None
    except Exception:
        pass
    return None, "没找到这个名称，试试6位代码或全称（如东华软件）"


def quote_item(code):
    code, err = resolve_code(code)
    if err:
        return None, err
    if len(code) != 6:
        return None, "代码必须是6位数字"
    first = infer_market(code)
    markets = [first, "sz" if first == "sh" else "sh"]
    for m in markets:
        try:
            live = rs.tencent([m + code])
        except Exception:
            live = {}
        q = live.get(code)
        if q and q.get("name") and q.get("px", 0) > 0:
            name = q["name"]
            kind = "etf" if ("ETF" in name.upper() or "基金" in name or code.startswith(("15", "51", "56", "58", "16"))) else "stock"
            return {
                "code": code,
                "market": m,
                "name": name,
                "px": q.get("px"),
                "chg": q.get("chg"),
                "kind": kind,
            }, None
    return None, "行情未找到该代码（检查市场或是否停牌）"


def read_wl():
    with open(WL_PATH, encoding="utf-8") as f:
        return json.load(f)


def find_in(data, code):
    for bucket in ("stocks", "etfs", "index"):
        for i, x in enumerate(data.get(bucket) or []):
            if x.get("code") == code:
                return bucket, i, x
    return None, None, None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[watch-api]", self.address_string(), fmt % args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    def _token(self):
        h = self.headers.get("Authorization") or ""
        if h.lower().startswith("bearer "):
            return h.split(" ", 1)[1].strip()
        return self.headers.get("X-Watch-Token") or ""

    def _need_auth(self):
        if token_ok(self._token()):
            return True
        self._json({"ok": False, "error": "需要登录"}, 401)
        return False

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/api/watchlist", "/watchlist"):
            if not self._need_auth():
                return
            self._json(read_wl())
            return
        if path in ("/api/quote", "/quote"):
            if not self._need_auth():
                return
            qs = parse_qs(urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            item, err = quote_item(code)
            if err:
                self._json({"ok": False, "error": err}, 400)
            else:
                self._json({"ok": True, "item": item})
            return
        if path in ("/api/snapshot", "/snapshot"):
            if not self._need_auth():
                return
            self._json(snapshot_status())
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        payload = self._read_json()
        if path in ("/api/login", "/login"):
            if password_ok(payload.get("password")):
                self._json({"ok": True, "token": new_token()})
            else:
                self._json({"ok": False, "error": "密码错误"}, 401)
            return
        if path in ("/api/analyze", "/analyze"):
            raw_q = str(payload.get("code") or payload.get("q") or payload.get("name") or "")
            code, err = resolve_code(raw_q)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            board = (payload.get("board") or "").strip() or "自选"
            kind = payload.get("kind") or ""
            save = bool(payload.get("save"))
            added = None
            if save:
                if not self._need_auth():
                    return
                item, err = quote_item(code)
                if err:
                    self._json({"ok": False, "error": err}, 400)
                    return
                with open(WL_PATH, "r+", encoding="utf-8") as lockf:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                    data = json.load(lockf)
                    bucket, _, existed = find_in(data, code)
                    if not existed:
                        rec = {"code": item["code"], "market": item["market"], "name": item["name"], "board": board}
                        use_kind = kind or item["kind"]
                        if use_kind == "etf":
                            rec.pop("board", None)
                            data.setdefault("etfs", []).append(rec)
                        else:
                            data.setdefault("stocks", []).append(rec)
                        data["updated"] = datetime.datetime.now(
                            datetime.timezone(datetime.timedelta(hours=8))
                        ).strftime("%Y-%m-%d")
                        lockf.seek(0)
                        lockf.truncate()
                        json.dump(data, lockf, ensure_ascii=False, indent=2)
                        lockf.write("\n")
                        added = rec
                    else:
                        added = existed
                        board = existed.get("board") or board
            try:
                out = rs.analyze_one(code, board, kind)
            except Exception as e:
                self._json({"ok": False, "error": "分析失败：" + str(e)[:180]}, 500)
                return
            if added:
                out["added"] = added
            self._json(out)
            return
        if not self._need_auth():
            return
        if path in ("/api/watchlist", "/watchlist", "/api/watchlist/add"):
            code, err = resolve_code(str(payload.get("code") or ""))
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            item, err = quote_item(code)
            if err:
                self._json({"ok": False, "error": err}, 400)
                return
            with open(WL_PATH, "r+", encoding="utf-8") as lockf:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                data = json.load(lockf)
                bucket, _, existed = find_in(data, code)
                if existed:
                    self._json({"ok": False, "error": f"已在自选：{existed.get('name')}", "item": existed}, 409)
                    return
                board = (payload.get("board") or "").strip() or "自选"
                rec = {"code": item["code"], "market": item["market"], "name": item["name"], "board": board}
                kind = payload.get("kind") or item["kind"]
                if kind == "etf":
                    rec.pop("board", None)
                    data.setdefault("etfs", []).append(rec)
                    rec["kind"] = "etf"
                else:
                    rec["kind"] = "stock"
                    data.setdefault("stocks", []).append(rec)
                data["updated"] = datetime.datetime.now(
                    datetime.timezone(datetime.timedelta(hours=8))
                ).strftime("%Y-%m-%d")
                lockf.seek(0)
                lockf.truncate()
                json.dump(data, lockf, ensure_ascii=False, indent=2)
                lockf.write("\n")
            self._json({"ok": True, "item": rec, "watchlist": data})
            return
        if path in ("/api/watchlist/remove", "/watchlist/remove"):
            code = re.sub(r"\D", "", str(payload.get("code") or ""))
            with open(WL_PATH, "r+", encoding="utf-8") as lockf:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
                data = json.load(lockf)
                bucket, idx, existed = find_in(data, code)
                if not existed:
                    self._json({"ok": False, "error": "自选里没有这只"}, 404)
                    return
                if bucket == "index":
                    self._json({"ok": False, "error": "指数不从这里删"}, 400)
                    return
                data[bucket].pop(idx)
                data["updated"] = datetime.datetime.now(
                    datetime.timezone(datetime.timedelta(hours=8))
                ).strftime("%Y-%m-%d")
                lockf.seek(0)
                lockf.truncate()
                json.dump(data, lockf, ensure_ascii=False, indent=2)
                lockf.write("\n")
            self._json({"ok": True, "removed": existed, "watchlist": data})
            return
        if path in ("/api/snapshot", "/snapshot", "/api/snapshot/run"):
            self._json(start_snapshot())
            return
        self._json({"error": "not found"}, 404)


def main():
    if not load_password():
        raise SystemExit("missing .watch_secret")
    port = int(os.environ.get("WATCH_API_PORT") or 8100)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"watch_api on 127.0.0.1:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
