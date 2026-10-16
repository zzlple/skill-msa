#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""msa Web 面板服务：把 msa skill 的分析结果用网页动态展示。

用法：
    python msa_server.py [--port 8765] [--host 127.0.0.1] [--open]

接口：
    GET  /                     面板页面（web/index.html）
    GET  /api/health           健康检查（用于单例探测）
    GET  /api/state            当前标的 / 频次 / 最近一次分析信息
    GET  /api/config           ?code=&shares=&cost=&interval=  设置参数
    GET  /api/analyze          ?code=&shares=&cost=&refresh=   执行一次分析，返回 JSON
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import msa_core  # noqa: E402
import msa_events  # noqa: E402

WEB_DIR = os.path.normpath(os.path.join(HERE, "..", "web"))

DEFAULT_PORT = int(os.environ.get("MSA_PORT", "8765"))
CACHE_TTL = float(os.environ.get("MSA_CACHE_TTL", "2.5"))

def _default_client():
    """启动时按运行容器动态识别调用方客户端名称。"""
    v = (os.environ.get("MSA_CLIENT_NAME") or "").strip()
    if v:
        return v
    try:
        import msa_env
        return msa_env.detect_container()
    except Exception:
        return "Marvis 电脑端"


# 本进程归属的客户端名：随 --client 固定，不随 /api/config 的临时注入改变，
# 供 msa.py 判断某个端口上的面板是否属于自己（避免多容器共用端口串台）。
OWNER = _default_client()


STATE_LOCK = threading.Lock()
STATE = {
    "client": _default_client(),
    "code": os.environ.get("MSA_CODE", "600519"),
    "market": None,
    "shares": 0,
    "cost": None,
    "refresh_fields": False,
    "interval": 15,
    "source": os.environ.get("MSA_QUOTE_SOURCE", msa_core.DEFAULT_QUOTE_SOURCE),
    "models": None,          # 默认全选，实际初值在 parse_models 定义后统一展开
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}
LAST = {"at": None, "ok": None, "error": None, "elapsed": None}
_CACHE = {"key": None, "ts": 0.0, "payload": None}


# --------------------------------------------------------------------------
# JSON 清洗
# --------------------------------------------------------------------------
def _jsonable(o):
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, bool) or o is None or isinstance(o, str):
        return o
    if isinstance(o, int):
        return o
    if isinstance(o, float):
        return None if (math.isnan(o) or math.isinf(o)) else o
    try:
        import numpy as np  # type: ignore

        if isinstance(o, np.floating):
            v = float(o)
            return None if (math.isnan(v) or math.isinf(v)) else v
        if isinstance(o, np.integer):
            return int(o)
        if isinstance(o, np.bool_):
            return bool(o)
        if isinstance(o, np.ndarray):
            return _jsonable(o.tolist())
    except Exception:
        pass
    return str(o)


def parse_models(raw):
    """解析 models 参数：'all' / '*' / 'none' / 'a,b,c' → 合法模型 id 列表。"""
    try:
        import msa_models
    except Exception:
        return []
    if raw in ("none", "-", "0"):
        return []
    if raw in ("all", "*"):
        return [m["id"] for m in msa_models.MODELS]
    valid = set(msa_models.MODEL_MAP)
    return [x.strip() for x in str(raw).split(",") if x.strip() in valid]


# 默认全选：未显式指定 MSA_MODELS 时，STATE 初始即承载全部模型 id
STATE["models"] = parse_models(os.environ.get("MSA_MODELS") or "all")


def run_analyze(code, shares=0, cost=None, refresh=False, source=None, models=None):
    """执行一次分析（带 2.5s 内同参数缓存，避免多标签页重复抓取）。"""
    key = (str(code), int(shares or 0), cost, bool(refresh), source,
           tuple(models or ()))
    now = time.time()
    with STATE_LOCK:
        if (not refresh and _CACHE["key"] == key
                and _CACHE["payload"] is not None
                and now - _CACHE["ts"] < CACHE_TTL):
            return _CACHE["payload"], 0.0, "cache"

    t0 = time.time()
    try:
        data = msa_core.analyze(code, market=None, shares=shares, cost=cost,
                                refresh_fields=bool(refresh), quiet=True,
                                source=source, models=models)
        payload = {"ok": True, "data": _jsonable(data)}
    except Exception as e:
        payload = {"ok": False, "error": "%s" % e,
                   "trace": traceback.format_exc()[-1500:]}
    elapsed = round(time.time() - t0, 2)

    with STATE_LOCK:
        _CACHE.update({"key": key, "ts": time.time(), "payload": payload})
        LAST.update({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "ok": payload["ok"],
                     "error": payload.get("error"), "elapsed": elapsed})
    return payload, elapsed, "live"


def is_cache_fresh():
    with STATE_LOCK:
        return (LAST["at"], LAST["ok"], LAST["elapsed"])


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MSA-Panel/1.0"

    def log_message(self, fmt, *args):  # 静音，日志写文件
        pass

    # -- helpers -----------------------------------------------------------
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _err(self, msg, code=500):
        self._json({"ok": False, "error": msg}, code)

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if path in ("/", "/index.html"):
                return self.page()
            if path == "/api/health":
                return self._json({"ok": True, "service": "msa-web", "version": 2,
                                   "owner": OWNER})
            if path == "/api/state":
                with STATE_LOCK:
                    st = dict(STATE)
                    st.update({"last": dict(LAST), "pid": os.getpid()})
                return self._json({"ok": True, "state": st})
            if path == "/api/sources":
                return self._json({"ok": True, "sources": msa_core.get_quote_sources(),
                                   "current": STATE.get("source") or msa_core.DEFAULT_QUOTE_SOURCE})
            if path == "/api/models":
                return self.models_api()
            if path == "/api/config":
                return self.config(q)
            if path == "/api/quote":
                return self.quote(q)
            if path == "/api/analyze":
                return self.analyze(q)
            if path == "/api/events":
                return self.events(q)
            if path in ("/favicon.ico",):
                return self._send(204, b"")
            return self._err("未知路径: %s" % path, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._err("%s" % e)
            except Exception:
                pass

    do_POST = do_GET

    def page(self):
        fp = os.path.join(WEB_DIR, "index.html")
        if not os.path.exists(fp):
            return self._err("缺少页面文件: %s" % fp, 500)
        with open(fp, "rb") as f:
            html = f.read().decode("utf-8")
        with STATE_LOCK:
            client = STATE.get("client") or "Marvis 电脑端"
        html = html.replace("{{CLIENT}}", client)
        self._send(200, html, "text/html; charset=utf-8")

    def models_api(self):
        """分析模型注册表 + 当前多选状态。"""
        try:
            import msa_models
        except Exception as e:
            return self._err("模型注册表加载失败: %s" % e)
        with STATE_LOCK:
            cur = list(STATE.get("models") or [])
        return self._json({"ok": True, "models": msa_models.model_list(),
                           "current": cur,
                           "default": list(msa_models.DEFAULT_MODELS),
                           "horizon": msa_models.HORIZON})

    def config(self, q):
        with STATE_LOCK:
            if q.get("client"):
                STATE["client"] = q["client"].strip()
            if q.get("code"):
                STATE["code"] = q["code"].strip()
            if q.get("shares") not in (None, ""):
                try:
                    STATE["shares"] = int(float(q["shares"]))
                except Exception:
                    pass
            if q.get("cost") not in (None, ""):
                try:
                    STATE["cost"] = float(q["cost"])
                except Exception:
                    pass
            if q.get("interval") not in (None, ""):
                try:
                    STATE["interval"] = max(5, int(float(q["interval"])))
                except Exception:
                    pass
            if q.get("source") not in (None, ""):
                src = q["source"].strip()
                if src in msa_core.QUOTE_SOURCES:
                    STATE["source"] = src
            if q.get("models") not in (None, ""):
                raw = q["models"].strip()
                ids = parse_models(raw)
                if ids or raw in ("none", "-", "0"):
                    STATE["models"] = ids
            st = dict(STATE)
            st["last"] = dict(LAST)
        return self._json({"ok": True, "state": st})

    def analyze(self, q):
        with STATE_LOCK:
            code = q.get("code") or STATE["code"]
            shares = q.get("shares")
            shares = STATE["shares"] if shares in (None, "") else int(float(shares))
            cost = q.get("cost")
            cost = STATE["cost"] if cost in (None, "") else float(cost)
            refresh = q.get("refresh") in ("1", "true", "True")
            source = q.get("source")
            if source not in (None, "") and source.strip() in msa_core.QUOTE_SOURCES:
                source = source.strip()
                STATE["source"] = source
            else:
                source = STATE.get("source") or msa_core.DEFAULT_QUOTE_SOURCE
            mraw = q.get("models")
            if mraw not in (None, ""):
                mraw = mraw.strip()
                mdls = parse_models(mraw)
                if mdls or mraw in ("none", "-", "0"):
                    STATE["models"] = mdls
                else:
                    mdls = list(STATE.get("models") or [])
            else:
                mdls = list(STATE.get("models") or [])
            STATE.update({"code": code, "shares": shares, "cost": cost,
                          "source": source})
        payload, elapsed, src = run_analyze(code, shares, cost, refresh, source, mdls)
        payload = dict(payload)
        payload.update({"elapsed": elapsed, "source": src,
                        "quote_source": source, "models": mdls,
                        "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})
        return self._json(payload)

    def quote(self, q):
        """快速取实时价（不跑全量分析），用于轮询静默刷新状态栏.

        真实取价：thsdk/wencai 源先拉 thsdk 分时/五档盘口构造 ctx，取价失败
        沿回退链 tencent → sina 继续；不再使用任何硬编码假价格。
        """
        with STATE_LOCK:
            code = q.get("code") or STATE["code"]
            source = q.get("source")
            if source not in (None, ""):
                source = source.strip()
            else:
                source = STATE.get("source") or msa_core.DEFAULT_QUOTE_SOURCE
        try:
            cd, mkt = msa_core._parse_cd(
                code, source if source in ("thsdk", "wencai") else "thsdk")
            if cd is None or mkt is None:
                return self._json({"ok": False, "error": "代码格式不合法"}, 400)
            ctx = {"intraday": None, "order_book": None, "wencai": {}}
            if source in ("thsdk", "wencai"):
                # 优先取 thsdk 分时（当日最新价），失败再退五档盘口卖一价
                if ctx["intraday"] is None:
                    try:
                        idf = msa_core.thsdk.intraday_data(mkt + cd)
                        if idf is not None and len(idf):
                            ctx["intraday"] = idf
                    except Exception:
                        pass
                if ctx["order_book"] is None:
                    try:
                        ob = msa_core.thsdk.list_security_order_books(
                            securities=[{"code": cd, "market": mkt}], depth="1")
                        if ob is not None and len(ob):
                            ctx["order_book"] = ob
                    except Exception:
                        pass
            price, meta = msa_core.fetch_live_price(source, code, mkt, ctx)
            # 回退链：thsdk/wencai 实时价不可得 → 腾讯 → 新浪
            if price is None:
                for fb in ("tencent", "sina"):
                    price, meta = msa_core.fetch_live_price(fb, code, mkt, ctx)
                    if price is not None:
                        source = fb
                        break
            if price is None:
                return self._json({"ok": False, "error": "取价失败"}, 400)
            return self._json({
                "ok": True,
                "price": price,
                "source": source,
                "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "meta": {"name": meta.get("name"),
                         "product": meta.get("product"),
                         "address": meta.get("address")},
            })
        except Exception as e:
            return self._json({"ok": False, "error": "%s" % e}, 500)

    def events(self, q):
        """基本面 / 突发事件评估：为纪律线提供外部参数（refresh=1 强制刷新，默认 30 分钟缓存）。"""
        with STATE_LOCK:
            code = (q.get("code") or STATE.get("code") or "").strip()
            market = q.get("market") or STATE.get("market") or ""
            name = (q.get("name") or "").strip()
            if name:
                STATE["name"] = name
            else:
                name = STATE.get("name") or ""
        try:
            res = msa_events.evaluate(
                code, name=name, market=market or "",
                force=q.get("refresh") in ("1", "true", "True"))
        except Exception as e:
            return self._json({"ok": False, "error": "%s" % e}, 500)
        res = dict(res)
        res["server_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return self._json(res)


def serve(port=DEFAULT_PORT, host="127.0.0.1"):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--open", action="store_true")
    ap.add_argument("--client", default=None,
                    help="调用方客户端名称（显示在浏览器选项卡标题）")
    args = ap.parse_args()

    if args.client:
        global OWNER
        OWNER = args.client.strip()
        with STATE_LOCK:
            STATE["client"] = OWNER

    log_dir = os.environ.get("MSA_LOG_DIR") or os.path.join(HERE, "..", ".run")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        log_dir = HERE

    httpd = serve(args.port, args.host)
    url = "http://%s:%d/" % (args.host, args.port)
    with open(os.path.join(log_dir, "msa_server.log"), "a", encoding="utf-8") as f:
        f.write("[%s] serve on %s pid=%d\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), url, os.getpid()))

    if args.open:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    print("msa panel: %s" % url, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
