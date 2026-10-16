# -*- coding: utf-8 -*-
"""
msa.py —— MSA 股票分析技能命令行入口

用法：
    # 单次分析（默认输出 Markdown + JSON 到 --out 目录）
    python msa.py --code 600519

    # 带持仓（给出持仓建议）
    python msa.py --code 600519 --shares 1000 --cost 1200

    # 每 1 分钟轮询分析（默认 interval=60 秒）
    python msa.py --code 600519 --shares 1000 --cost 1200 --watch --interval 60

    # ETF / 基金（自动识别，无需指定 market）
    python msa.py --code 510300          # 沪深300ETF（沪市基金）
    python msa.py --code 159915          # 创业板ETF（深市基金）
    python msa.py --code 161725          # 白酒基金 LOF

参数：
    --code      证券代码，支持沪深A股 600519/000001/300033，ETF/基金 510300/159915/161725，
                以及 600519.SH / USHA600519 等全码写法
    --market    可选，强制指定市场（USHA/USZA/USTM/UHKG…）
    --shares    持仓数量（股）
    --cost      持仓成本价（元）
    --out       产出目录（默认当前目录）
    --watch     开启轮询模式
    --interval  轮询间隔秒数，默认 60
    --rounds    轮询次数，0 表示不限（配合 Ctrl+C 停止）
    --json      额外把完整结果以 JSON 打印到终端
    --refresh-fields  强制刷新问财字段元数据缓存
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import msa_core as core  # noqa: E402


def _ts(fmt="%Y%m%d_%H%M%S"):
    return datetime.now(core.TZ_CN).strftime(fmt)


# --------------------------------------------------------------------------
# Web 面板（动态展示）
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_PY = os.path.join(HERE, "msa_server.py")
SESSION_FILE = os.path.join(HERE, "..", ".run", "panel_session.json")
PORT_CANDIDATES = [8765, 8766, 8767, 8768]


def _http_json(url, timeout=1.0):
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _panel_port(preferred=None, timeout=0.5, owner=None):
    """返回已在运行、且归属当前客户端的面板端口；没有则返回 None。

    owner 为当前容器名时会跳过其它客户端（Marvis / WorkBuddy / 豆包）起的面板，
    避免多容器共用同一端口导致标题与数据串台。
    """
    ports = ([preferred] if preferred else []) + PORT_CANDIDATES
    for p in dict.fromkeys(ports):
        try:
            info = _http_json("http://127.0.0.1:%d/api/health" % p, timeout)
        except Exception:
            continue
        if info.get("service") != "msa-web":
            continue
        if owner and str(info.get("owner") or "") != str(owner):
            continue
        return p
    return None


def _free_port(preferred=None, extra=8):
    """挑一个可绑定的端口：优先 preferred，其次候选表，再往后顺延。"""
    import socket
    cands = ([preferred] if preferred else []) + PORT_CANDIDATES
    for p in dict.fromkeys(cands):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", int(p)))
            return int(p)
        except Exception:
            pass
        finally:
            try:
                s.close()
            except Exception:
                pass
    base = max(PORT_CANDIDATES)
    for p in range(base + 1, base + 1 + extra):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except Exception:
            continue
        finally:
            try:
                s.close()
            except Exception:
                pass
    return base


def _read_session():
    try:
        return json.load(open(SESSION_FILE, "r", encoding="utf-8"))
    except Exception:
        return {}


def _write_session(d):
    try:
        os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
        json.dump(d, open(SESSION_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    except Exception:
        pass


def _client_name(args=None):
    """调用方客户端名：--client > MSA_CLIENT_NAME > 运行容器自动识别 > 兜底。"""
    v = (getattr(args, "client", None) or os.environ.get("MSA_CLIENT_NAME") or "").strip()
    if v:
        return v
    try:
        import msa_env
        return msa_env.detect_container()
    except Exception:
        return "Marvis 电脑端"


def ensure_panel(log_dir=None, port=None, wait=6.0, client=None):
    """确保当前客户端的面板服务在运行，返回端口号。"""
    p = _panel_port(port, owner=client)
    if p:
        return p
    import subprocess
    env = dict(os.environ)
    if log_dir:
        env["MSA_LOG_DIR"] = log_dir
    logf = open(os.path.join(log_dir or HERE, "msa_panel_boot.log"), "a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    target = _free_port(port)
    cmd = [sys.executable, SERVER_PY, "--port", str(target)]
    if client:
        cmd += ["--client", client]
    subprocess.Popen(cmd,
                     env=env, stdout=logf, stderr=subprocess.STDOUT,
                     creationflags=flags, close_fds=True)
    t0 = time.time()
    while time.time() - t0 < wait:
        p = _panel_port(target, timeout=0.4, owner=client)
        if p:
            return p
        time.sleep(0.25)
    raise RuntimeError("面板服务启动超时（日志见 %s）" % logf.name)


def panel_set(port, code, shares=0, cost=None, interval=None, client=None):
    q = "code=%s&shares=%s&cost=%s" % (code, shares, "" if cost is None else cost)
    if interval:
        q += "&interval=%d" % interval
    if client:
        import urllib.parse
        q += "&client=%s" % urllib.parse.quote(client)
    try:
        _http_json("http://127.0.0.1:%d/api/config?%s" % (port, q), timeout=2.0)
    except Exception:
        pass


def panel_open(port, code, shares=0, cost=None, interval=None, dedup_sec=120):
    """打开浏览器面板；同一标的短期内不重复弹标签页。"""
    sess = _read_session()
    now = time.time()
    same = (sess.get("code") == str(code) and sess.get("port") == port
            and now - float(sess.get("ts") or 0) < dedup_sec)
    if same:
        return False
    url = "http://127.0.0.1:%d/?code=%s&shares=%s&cost=%s" % (
        port, code, shares, "" if cost is None else cost)
    if interval:
        url += "&interval=%d" % interval
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        return False
    _write_session({"code": str(code), "port": port, "ts": now})
    return True


def start_panel(args, code, log_dir=None):
    """按参数启动/唤起面板。返回端口或 None。"""
    if getattr(args, "no_web", False):
        return None
    try:
        client = _client_name(args)
        port = ensure_panel(log_dir=log_dir, port=getattr(args, "port", None), client=client)
        panel_set(port, code, args.shares, args.cost, args.interval, client=client)
        opened = panel_open(port, code, args.shares, args.cost, args.interval)
        print("  → 面板: http://127.0.0.1:%d/%s" % (port, "（已唤起浏览器）" if opened else "（已运行中，页面自动刷新）"))
        return port
    except Exception as exc:
        print("  （Web 面板未启动：%s）" % exc)
        return None


def run_once(code, args, outdir, tag=""):
    res = core.analyze(code, market=args.market, shares=args.shares,
                       cost=args.cost, refresh_fields=args.refresh_fields)
    md = core.render_markdown(res)
    os.makedirs(outdir, exist_ok=True)
    stamp = _ts()
    name = res["meta"]["code"]
    md_path = os.path.join(outdir, "msa_%s%s.md" % (name, tag))
    json_path = os.path.join(outdir, "msa_%s%s_latest.json" % (name, tag))
    open(md_path, "w", encoding="utf-8").write(md)
    json.dump(res, open(json_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1,
              default=str)
    print(core.compact_summary(res))
    print("  → 报告: %s" % md_path)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
    return res, md_path, json_path


def main():
    ap = argparse.ArgumentParser(description="MSA 股票 / ETF / 基金分析与持仓建议")
    ap.add_argument("--code", required=True,
                    help="证券代码：沪深A股 600519/000001，ETF/基金 510300/159915/161725")
    ap.add_argument("--market", default=None)
    ap.add_argument("--shares", type=int, default=0)
    ap.add_argument("--cost", type=float, default=None)
    ap.add_argument("--out", default=os.getcwd())
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--rounds", type=int, default=0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--refresh-fields", action="store_true")
    ap.add_argument("--no-web", action="store_true",
                    help="不启动/不唤起 Web 动态面板（默认每次调用都会启动并唤起）")
    ap.add_argument("--port", type=int, default=None, help="面板端口，默认 8765")
    ap.add_argument("--client", default=None,
                    help="调用方客户端名称，显示在面板标题（默认按运行容器自动识别）")
    ap.add_argument("--calm", action="store_true",
                    help="轮询模式下仅在评分/方向发生变化时打印完整摘要")
    args = ap.parse_args()

    panel_port = start_panel(args, args.code, log_dir=getattr(args, "log_dir", None))

    outdir = os.path.abspath(args.out)
    if not args.watch:
        try:
            run_once(args.code, args, outdir)
        except KeyboardInterrupt:
            sys.exit(1)
        except Exception as exc:
            print("分析失败：%s" % exc)
            sys.exit(2)
        return

    print("MSA 轮询模式启动：每 %d 秒分析一次，共 %s 次（0=不限，Ctrl+C 退出）"
          % (args.interval, args.rounds or "不限"))
    log_path = os.path.join(outdir, "msa_watch_%s.log" % _ts("%Y%m%d"))
    os.makedirs(outdir, exist_ok=True)
    i = 0
    last_key = None
    while True:
        i += 1
        print("\n===== 第 %d 次分析 @ %s =====" % (i, _ts("%Y-%m-%d %H:%M:%S")))
        if panel_port:
            panel_set(panel_port, args.code, args.shares, args.cost, args.interval,
                      client=_client_name(args))
        try:
            res, md_path, json_path = run_once(args.code, args, outdir)
            key = tuple((k, res["forecast"][k].get("评分")) for k in ("日", "周", "月"))
            line = core.compact_summary(res).replace("\n", " | ")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write("%s\t%s\n" % (_ts("%Y-%m-%d %H:%M:%S"), line))
            if args.calm and key == last_key:
                print("  （与上次一致，已记录日志）")
            last_key = key
        except KeyboardInterrupt:
            print("\n已手动终止轮询。")
            break
        except Exception as exc:
            print("  本轮分析失败：%s" % exc)
        if args.rounds and i >= args.rounds:
            print("\n已完成 %d 次分析，退出轮询。" % i)
            break
        try:
            time.sleep(max(5, args.interval))
        except KeyboardInterrupt:
            print("\n已手动终止轮询。")
            break
    print("轮询日志: %s" % log_path)


if __name__ == "__main__":
    main()
