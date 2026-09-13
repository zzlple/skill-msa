# -*- coding: utf-8 -*-
"""
msa_core —— MSA 股票分析与持仓建议核心库

数据源：thsdk 3.x（同花顺，游客模式）
权限边界：
    - 沪深A股（USHA/USZA）：数据完整。
    - ETF/LOF/基金（USHJ 沪基金 / USZJ 深基金）：K线、分时、逐笔、五档盘口、分价表均可用；
      资金流接口（list_security_daily_capital_flows）对基金标的会 encode_failed，改用
      分价表 + 逐笔成交推导主力资金；基金专有字段（规模/净值/折溢价/份额/跟踪指数等）
      通过问财实时字段接口获取。
    - 市场代码由 thsdk.complete_ths_code 权威解析（如 510300 -> USHJ510300），失败时回退本地规则。
    - 北交所（USTA）、B股（USHB/USZB）、港股等仍为部分接口可用，缺数据在 warnings 中说明。

对外主入口：
    analyze(code, market=None, shares=0, cost=None, verbose=False) -> dict
    render_markdown(result) -> str
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import pandas as pd

TZ_CN = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(TZ_CN)


try:
    import thsdk
except ImportError:  # pragma: no cover
    raise ImportError("缺少依赖 thsdk，请先执行：pip install --upgrade thsdk")

_AUTH = {"ok": False}


def ensure_auth():
    if not _AUTH["ok"]:
        thsdk.auth()
        _AUTH["ok"] = True


# --------------------------------------------------------------------------
# 问财实时字段
# --------------------------------------------------------------------------
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
FIELD_CACHE = os.path.join(CACHE_DIR, "wencai_fields.json")

WENCAI_NAMES = [
    # 盘面/量价
    "涨速", "均价", "换手率", "量比", "委比", "委差", "振幅", "外盘",
    "成交量", "成交额", "涨停价", "跌停价", "涨跌幅", "现价",
    # 资金/大单
    "大单净量", "主力净额", "超大单净额", "分时ddx",
    # 技术
    "dif", "dea", "macd", "atr",
    # 估值/股本
    "市盈率", "市净率", "市销率", "总市值", "流通市值", "股息率", "总股本", "流通股本",
    # 区间涨幅
    "5日涨幅", "10日涨幅", "20日涨幅", "60日涨幅", "年初至今涨幅",
    # 基金/ETF 专项（沪深A股返回空）
    "实时估值", "最新份额", "跟踪指数", "折价率", "跟踪误差",
    # 多口径字段（取默认口径）
    "今昨成交比", "每股收益", "每股净资产", "净利润", "营业收入", "净利率", "每股公积金",
]

# 基金/ETF 专有字段（仅基金标的返回有效值，A股为空；"跟踪误差率" 当前问财无对应元数据）
FUND_WENCAI_NAMES = [
    "基金规模", "单位净值", "最新净值", "参考净值", "累计净值",
    "折价率", "折溢价率", "基金份额", "场内份额",
    "跟踪指数", "跟踪指数代码", "管理费率",
    "上市日期", "基金成立日", "基金类型",
]

# 基金/ETF 所属市场：沪基金 USHJ、深基金 USZJ
FUND_MARKETS = ("USHJ", "USZJ")


def is_fund(market, code=None):
    """判断标的是否为基金/ETF/LOF。"""
    if str(market or "").upper() in FUND_MARKETS:
        return True
    cd = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(cd) == 6:
        if cd.startswith(("51", "52", "53", "56", "58", "50", "501", "502", "503", "505", "506", "508")):
            return True          # 沪市 ETF / LOF / REITs
        if cd.startswith(("15", "16", "18")):
            return True          # 深市 ETF / LOF / 封闭式基金
    return False


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def _pick_meta(recs, name):
    def score(r):
        s = 0
        d = str(r.get("display_name") or "")
        if r.get("query_group") == name:
            s += 4
        if r.get("show_name") == name:
            s += 2
        if d.startswith(name):
            s += 1
        if r.get("timestamp"):
            s += 1
        return s

    return max(recs, key=lambda r: (score(r), str(r.get("display_name") or "")))


def resolve_fields(names=None, refresh=False, quiet=True):
    """解析问财字段元数据（带本地缓存），返回 {字段名: meta or None}。"""
    names = list(names or WENCAI_NAMES)
    cache = {}
    if os.path.exists(FIELD_CACHE):
        try:
            cache = json.load(open(FIELD_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    todo = [n for n in names if refresh or n not in cache]
    if todo:
        ensure_auth()
        for n in todo:
            try:
                df = thsdk.list_wencai_expression_fields(n)
                if df is None or len(df) == 0:
                    cache.setdefault(n, None)
                    continue
                recs = [{k: _clean(v) for k, v in row.to_dict().items()}
                        for _, row in df.iterrows()]
                cache[n] = _pick_meta(recs, n)
            except Exception as exc:
                if not quiet:
                    print("[resolve] %s 失败: %s" % (n, exc))
                cache.setdefault(n, None)
            time.sleep(0.05)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            json.dump(cache, open(FIELD_CACHE, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception:
            pass
    return cache


def query_wencai(code, market, names, cache, warnings=None):
    """批量取问财实时字段值，返回 {字段名: 值}。"""
    metas = []
    for n in names:
        m = cache.get(n)
        if not m:
            continue
        m = dict(m)
        m["query_group"] = m.get("query_group") or n
        m["show_name"] = n
        m["display_name"] = n
        metas.append((n, m))
    out = {}
    sec = {"code": code, "market": market}
    for i in range(0, len(metas), 8):
        chunk = metas[i:i + 8]
        try:
            r = thsdk.query_wencai_realtime_fields(securities=[sec],
                                                   fields=[m for _, m in chunk])
            if r is None or len(r) == 0:
                continue
            cells = r.iloc[0].get("cells") or []
            for idx, c in enumerate(cells):
                nm = c.get("name")
                if nm not in names and idx < len(chunk):
                    nm = chunk[idx][0]
                if nm in names:
                    out[nm] = _clean(c.get("value"))
        except Exception as exc:
            # 整组失败：改成逐字段重试，避免个别不适用字段（如 ETF 专有字段）拖垮整组
            got = 0
            for nm, m in chunk:
                try:
                    r = thsdk.query_wencai_realtime_fields(securities=[sec], fields=[m])
                    if r is not None and len(r):
                        cells = r.iloc[0].get("cells") or []
                        if cells:
                            out[nm] = _clean(cells[0].get("value"))
                            got += 1
                except Exception:
                    pass
                time.sleep(0.1)
            if got == 0 and warnings is not None:
                warnings.append("问财字段分组 %d 无数据: %s" % (i, str(exc)[:60]))
        time.sleep(0.1)
    return out


# --------------------------------------------------------------------------
# 代码解析
# --------------------------------------------------------------------------
MARKET_PREFIXES = ("USHA", "USZA", "USTM", "USTA", "USHI", "USZI", "USHJ", "USZJ",
                   "USHB", "USZB", "UHKG", "UNQQ", "UFXB", "USHD", "URFI", "UCFS")

_CODE_CACHE = {}


def _fallback_market(d):
    """thsdk 解析不可用时的本地回退规则。"""
    if d[0] == "6":
        return "USHA", d
    if d[0] in ("0", "3"):
        return "USZA", d
    if d[0] in ("4", "8"):
        return "USTA", d            # 北交所
    if d[0] == "5":
        return "USHJ", d            # 沪市基金 / ETF / REITs
    if d[0] == "1":
        return "USZJ", d            # 深市基金 / ETF / LOF
    if d[0] == "9":
        return "USHB", d            # 沪B
    if d[0] == "2":
        return "USZB", d            # 深B
    if len(d) <= 5:
        return "UHKG", d.zfill(5)
    return "USHA", d


def parse_code(raw, resolve=True):
    """返回 (market, code)。支持 600519 / 600519.SH / 000001.SZ / 510300 / USHJ510300。

    优先用 thsdk.complete_ths_code 权威解析（可正确识别 ETF/基金/B股/北交所等），
    解析不可用时回退本地规则。
    """
    s = str(raw).strip().upper().replace(" ", "")
    for p in MARKET_PREFIXES:
        if s.startswith(p) and len(s) > len(p):
            return p, s[len(p):]
    if "." in s:
        body, suf = s.split(".", 1)
        m = {"SH": "USHA", "SS": "USHA", "SZ": "USZA", "BJ": "USTA", "HK": "UHKG",
             "OF": "USHJ"}
        if suf in m:
            body = body.zfill(5) if suf == "HK" else body
            return m[suf], body
        s = body
    d = "".join(ch for ch in s if ch.isdigit())
    if not d:
        raise ValueError("无法识别的股票代码: %s" % raw)
    if d in _CODE_CACHE:
        return _CODE_CACHE[d]
    if resolve:
        try:
            ensure_auth()
            r = thsdk.complete_ths_code(d)
            if r is not None and len(r):
                mk = str(r.iloc[0].get("market") or "").upper()
                if mk:
                    _CODE_CACHE[d] = (mk, d)
                    return mk, d
        except Exception:
            pass
    mk, cd = _fallback_market(d)
    _CODE_CACHE[d] = (mk, cd)
    return mk, cd


# --------------------------------------------------------------------------
# 技术指标
# --------------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def macd(close):
    dif = ema(close, 12) - ema(close, 26)
    dea = dif.ewm(span=9, adjust=False).mean()
    return dif, dea, (dif - dea) * 2


def kdj(df, n=9):
    low_n = df["low"].rolling(n).min()
    high_n = df["high"].rolling(n).max()
    rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, float("nan")) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    d = k.ewm(com=2, adjust=False).mean()
    return k, d, 3 * k - 2 * d


def rsi(close, n):
    delta = close.diff()
    up = delta.clip(lower=0)
    dn = (-delta).clip(lower=0)
    rs = up.ewm(alpha=1.0 / n, adjust=False).mean() / dn.ewm(alpha=1.0 / n, adjust=False).mean()
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - df["close"].shift()).abs(),
                    (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0 / n, adjust=False).mean()


def trendline(closes, sigma=1.5):
    """最小二乘趋势线：斜率、方向、上下轨（含下一期预测值）。"""
    y = [float(c) for c in closes]
    n = len(y)
    if n < 3:
        return None
    x = list(range(n))
    mx = sum(x) / n
    my = sum(y) / n
    den = sum((xi - mx) ** 2 for xi in x)
    slope = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / den if den else 0.0
    intercept = my - slope * mx
    resid = [yi - (slope * xi + intercept) for xi, yi in zip(x, y)]
    sd = (sum(r * r for r in resid) / n) ** 0.5
    nxt = intercept + slope * n
    return {
        "slope": slope,
        "slope_pct": (slope / my * 100) if my else 0.0,
        "sd": sd,
        "next": nxt,
        "fit_last": intercept + slope * (n - 1),
        "upper": nxt + sigma * sd,
        "lower": nxt - sigma * sd,
        "direction": "上行" if slope > 0.0008 * my else ("下行" if slope < -0.0008 * my else "走平"),
        "sample": n,
    }


# 港股/港股通基金：净值多为个位数（如 159567 现价 0.666），统一 3 位小数；
# 其余标的仍按量级自适应（>=100→2、10~100→3、<10→4）。
_PXD = threading.local()

_HK_KEYS = ("港股", "恒生", "H股", "中概", "港股通", "沪深港", "港币", "香港", "恒指")


def _is_hk_fund(name, tracking=None):
    """标的名称或跟踪指数命中港股关键词即视为港股基金。"""
    txt = "%s %s" % (name or "", tracking or "")
    return any(k in txt for k in _HK_KEYS)


def _set_price_dec(nd):
    _PXD.nd = nd


def _price_dec():
    return getattr(_PXD, "nd", None)


def _px(v, nd=None):
    """价格原样精度：按量级保留真实小数位（基金净值 4 位、百元股 2 位），不做万/亿折算。
    港股/港股通基金由 _set_price_dec 指定固定 3 位小数。"""
    if v is None:
        return None
    try:
        x = float(v)
    except Exception:
        return None
    if nd is None:
        nd = _price_dec()
    if nd is None:
        a = abs(x)
        nd = 4 if a < 10 else (3 if a < 100 else 2)
    return round(x, nd)


def _f(v, nd=2):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except Exception:
        return None


# --------------------------------------------------------------------------
# 数据采集
# --------------------------------------------------------------------------
def _safe(fn, warnings, tag, default=None):
    try:
        return fn()
    except Exception as exc:
        if warnings is not None:
            warnings.append("%s 获取失败: %s" % (tag, str(exc)[:80]))
        return default


def collect(code, market, warnings, cache):
    ctx = {}

    # 名称
    try:
        nm = thsdk.sort_securities(securities=[{"code": code, "market": market}])
        ctx["name"] = str(nm.iloc[0]["name"]) if len(nm) else code
    except Exception:
        ctx["name"] = code

    ctx["full_code"] = market + code

    # K线
    ctx["day"] = _safe(lambda: thsdk.klines(ctx["full_code"], interval="day", count=260,
                                            adjust="forward"), warnings, "日K")
    ctx["day_raw"] = _safe(lambda: thsdk.klines(ctx["full_code"], interval="day", count=5),
                           warnings, "日K(不复权)")
    ctx["week"] = _safe(lambda: thsdk.klines(ctx["full_code"], interval="week", count=120,
                                             adjust="forward"), warnings, "周K")
    ctx["month"] = _safe(lambda: thsdk.klines(ctx["full_code"], interval="month", count=60,
                                              adjust="forward"), warnings, "月K")
    ctx["min5"] = _safe(lambda: thsdk.klines(ctx["full_code"], interval="5m", count=100),
                        warnings, "5分钟K")
    ctx["intraday"] = _safe(lambda: thsdk.intraday_data(ctx["full_code"]), warnings, "分时")

    # 盘口 / 大单 / 分价 / 逐笔 / 财务快照
    fund = is_fund(market, code)
    ctx["is_fund"] = fund
    ctx["order_book"] = _safe(lambda: thsdk.list_security_order_books(
        securities=[{"code": code, "market": market}], depth="5"), warnings, "五档盘口")
    ctx["pvl"] = _safe(lambda: thsdk.list_security_price_volume_levels(
        security={"code": code, "market": market}), warnings, "分价表")
    ctx["ticks"] = _safe(lambda: thsdk.tick_level1(ctx["full_code"], count=120), warnings, "逐笔成交")
    if fund:
        # 基金标的不适用：资金流接口 encode_failed、财务快照 no_data，直接跳过，
        # 主力资金改由分价表 + 逐笔成交推导（见 build_flows）
        ctx["flows"] = None
        ctx["fin"] = None
        ctx["flows_na"] = "资金流接口不支持基金/ETF，已改用分价表+逐笔成交推导主动买卖"
    else:
        ctx["flows"] = _safe(lambda: thsdk.list_security_daily_capital_flows(
            securities=[{"code": code, "market": market}]), warnings, "资金流")
        ctx["fin"] = _safe(lambda: thsdk.list_security_financial_snapshots(
            securities=[{"code": code, "market": market}],
            fields=["total_shares", "float_shares", "net_asset_per_share"]), warnings, "财务快照")

    # 问财实时字段（基金标的追加基金专有字段）
    sec = {"code": code, "market": market}
    ctx["wencai"] = {}
    want = list(WENCAI_NAMES) + (list(FUND_WENCAI_NAMES) if fund else [])
    try:
        ctx["wencai"] = query_wencai(code, market, want, cache, warnings)
    except Exception as exc:
        warnings.append("问财字段查询失败: %s" % str(exc)[:80])
    ctx["sec"] = sec
    return ctx


# --------------------------------------------------------------------------
# 计算：行情 / 指标 / 资金
# --------------------------------------------------------------------------
def build_quote(ctx):
    day = ctx.get("day")
    raw = ctx.get("day_raw")
    q = {}
    if raw is not None and len(raw):
        last = raw.iloc[-1]
        # 价格类字段走 _px：按量级保留源数据真实小数位，不做 2 位四舍五入
        # （ETF/基金净值常为 3~4 位小数，如 0.665 若 round 到 2 位会失真为 0.67）
        q["今开"] = _px(last.get("open"))
        q["最高"] = _px(last.get("high"))
        q["最低"] = _px(last.get("low"))
        q["收盘"] = _px(last.get("close"))
        q["总手"] = _f(last.get("volume"), 0)
        q["金额"] = _f(last.get("turnover"), 0)
        if len(raw) >= 2:
            q["昨收"] = _px(raw.iloc[-2].get("close"))
        q["date"] = str(raw.index[-1])[:10]
    w = ctx.get("wencai") or {}
    if w.get("现价") is not None:
        q["现价"] = _px(w["现价"])
    if q.get("收盘") and q.get("昨收"):
        q["涨跌幅"] = _f((q["收盘"] / q["昨收"] - 1) * 100)
        q["涨跌额"] = _px(q["收盘"] - q["昨收"])
    if w.get("涨跌幅") is not None:      # 前复权口径更贴近行情软件
        q["涨跌幅"] = _f(w["涨跌幅"])
    # 比例/量能类：保留 2 位
    for k_src, k_dst in [("涨跌幅", "涨跌幅"), ("涨速", "涨速"), ("量比", "量比"),
                         ("换手率", "换手率"), ("振幅", "振幅"), ("委比", "委比"),
                         ("委差", "委差"), ("外盘", "外盘"),
                         ("成交量", "成交量"), ("成交额", "成交额")]:
        if w.get(k_src) is not None and k_dst not in q:
            q[k_dst] = _f(w[k_src])
    # 价位类：走 _px 保真实精度
    for k_src, k_dst in [("均价", "均价"), ("均价:不复权", "均价"),
                         ("涨停价", "涨停价"), ("跌停价", "跌停价")]:
        if w.get(k_src) is not None and k_dst not in q:
            q[k_dst] = _px(w[k_src])
    if q.get("均价") is None and q.get("金额") and q.get("总手"):
        q["均价"] = _px(q["金额"] / q["总手"])
    # 内盘 = 总量 - 外盘
    if q.get("外盘") is not None and q.get("成交量"):
        try:
            q["内盘"] = _f(q["成交量"] - q["外盘"], 0)
        except Exception:
            pass
    return q


def build_indicators(ctx):
    day = ctx.get("day")
    ind = {}
    if day is None or len(day) < 20:
        return ind
    df = day.copy()
    close = df["close"]
    for n in (5, 10, 20, 60, 120):
        if len(df) >= n:
            ind["MA%d" % n] = _px(close.rolling(n).mean().iloc[-1])
    dif, dea, m = macd(close)
    ind["DIF"] = _f(dif.iloc[-1], 3)
    ind["DEA"] = _f(dea.iloc[-1], 3)
    ind["MACD(M)"] = _f(m.iloc[-1], 3)
    ind["MACD金叉"] = bool(len(df) > 1 and dif.iloc[-1] > dea.iloc[-1] and dif.iloc[-2] <= dea.iloc[-2])
    ind["MACD多头"] = bool(dif.iloc[-1] > dea.iloc[-1])
    k, d, j = kdj(df)
    ind["KDJ-K"] = _f(k.iloc[-1])
    ind["KDJ-D"] = _f(d.iloc[-1])
    ind["KDJ-J"] = _f(j.iloc[-1])
    ind["KDJ金叉"] = bool(len(df) > 1 and k.iloc[-1] > d.iloc[-1] and k.iloc[-2] <= d.iloc[-2])
    for n in (6, 12, 24):
        if len(df) > n:
            ind["RSI%d" % n] = _f(rsi(close, n).iloc[-1])
    at = atr(df, 14)
    ind["ATR14"] = _f(at.iloc[-1])
    ind["ATR14%"] = _f(at.iloc[-1] / close.iloc[-1] * 100)
    # 量能
    ind["MA5量"] = _f(df["volume"].rolling(5).mean().iloc[-1], 0)
    ind["MA10量"] = _f(df["volume"].rolling(10).mean().iloc[-1], 0)
    if len(df) >= 2 and df["volume"].iloc[-2]:
        ind["今昨成交比"] = _f(df["volume"].iloc[-1] / df["volume"].iloc[-2])
    # 位置
    hi20 = df["high"].iloc[-20:].max()
    lo20 = df["low"].iloc[-20:].min()
    hi60 = df["high"].iloc[-60:].max() if len(df) >= 60 else df["high"].max()
    lo60 = df["low"].iloc[-60:].min() if len(df) >= 60 else df["low"].min()
    ind["近20日高"] = _px(hi20)
    ind["近20日低"] = _px(lo20)
    ind["近60日高"] = _px(hi60)
    ind["近60日低"] = _px(lo60)
    ind["近60日高距%"] = _f((close.iloc[-1] / hi60 - 1) * 100) if hi60 else None
    ind["近60日低距%"] = _f((close.iloc[-1] / lo60 - 1) * 100) if lo60 else None
    return ind


def build_periods(ctx):
    """7 / 14 / 28 / 57 日区间情况。"""
    day = ctx.get("day")
    out = {}
    if day is None or len(day) < 8:
        return out
    df = day
    for n in (7, 14, 28, 57):
        if len(df) < n + 1:
            continue
        seg = df.iloc[-(n + 1):]
        c0 = seg["close"].iloc[0]
        out["%d日" % n] = {
            "区间涨跌幅%": _f((seg["close"].iloc[-1] / c0 - 1) * 100),
            "区间振幅%": _f((seg["high"].max() / seg["low"].min() - 1) * 100),
            "日均量": _f(seg["volume"].iloc[1:].mean(), 0),
            "日均额": _f(seg["turnover"].iloc[1:].mean(), 0),
            "区间最高": _px(seg["high"].max()),
            "区间最低": _px(seg["low"].min()),
            "量能比(近段/全段)": None,
        }
    return out


def build_order_book(ctx):
    ob = ctx.get("order_book")
    res = {"买1-5": [], "卖1-5": [], "买卖差": None, "买卖力道": None}
    if ob is None or len(ob) == 0:
        return res
    row = ob.iloc[0]
    bids = list(row.get("bids") or [])
    asks = list(row.get("asks") or [])
    res["买1-5"] = [{"档": "买%d" % (i + 1), "价": _px(b.get("price")), "量(手)": _f(b.get("volume"), 0)}
                    for i, b in enumerate(bids[:5])]
    res["卖1-5"] = [{"档": "卖%d" % (i + 1), "价": _px(a.get("price")), "量(手)": _f(a.get("volume"), 0)}
                    for i, a in enumerate(asks[:5])]
    bv = sum(float(b.get("volume") or 0) for b in bids[:5])
    sv = sum(float(a.get("volume") or 0) for a in asks[:5])
    res["买五量(手)"] = _f(bv, 0)
    res["卖五量(手)"] = _f(sv, 0)
    res["买卖差"] = _f(bv - sv, 0)
    res["买卖力道"] = _f((bv - sv) / (bv + sv) * 100) if (bv + sv) else None
    return res


def _derive_fund_flows(ctx, quote):
    """基金/ETF 无资金流接口：用分价表 + 逐笔成交推导主动买卖（近似同花顺口径）。"""
    def nz(x):
        try:
            f = float(x)
            return 0.0 if math.isnan(f) or math.isinf(f) else f
        except Exception:
            return 0.0

    res = {}
    turnover = buy_amt = sell_amt = vol_sum = 0.0
    pvl = ctx.get("pvl")
    if pvl is not None and len(pvl):
        for _, r in pvl.iterrows():
            p = nz(r.get("price"))
            v = nz(r.get("volume"))
            turnover += p * v
            vol_sum += v
            buy_amt += p * nz(r.get("in_volume"))
            sell_amt += p * nz(r.get("out_volume"))

    big_buy = big_sell = 0.0
    tk = ctx.get("ticks")
    if tk is not None and len(tk):
        prev = None
        for _, r in tk.iterrows():
            p = nz(r.get("price"))
            amt = p * nz(r.get("volume"))
            dt = r.get("deal_type")
            if dt == 1:
                side = "买"
            elif dt == 2:
                side = "卖"
            elif prev is not None and p > prev:
                side = "买"
            elif prev is not None and p < prev:
                side = "卖"
            else:
                side = "中性"
            prev = p
            if amt >= 1_000_000:
                if side == "买":
                    big_buy += amt
                elif side == "卖":
                    big_sell += amt

    turnover = turnover or nz(quote.get("金额"))
    net = buy_amt - sell_amt
    res["主力净额"] = _f(net, 0)                       # 主买额-主卖额（分价口径）
    res["主力净占比%"] = _f(net / turnover * 100) if turnover else None
    res["主买额"] = _f(buy_amt, 0)
    res["主卖额"] = _f(sell_amt, 0)
    res["大单净额"] = _f(big_buy - big_sell, 0)        # 单笔≥100万（逐笔口径）
    res["大单买入额"] = _f(big_buy, 0)
    res["大单卖出额"] = _f(big_sell, 0)
    res["成交额"] = _f(turnover, 0)
    res["加权均价"] = _px(turnover / vol_sum) if vol_sum else None
    res["口径"] = ctx.get("flows_na") or "按分价表+逐笔成交推导（近似）"
    return res


def build_flows(ctx, quote):
    fl = ctx.get("flows")
    res = {}
    if fl is None or len(fl) == 0:
        if ctx.get("is_fund"):
            return _derive_fund_flows(ctx, quote)
        return res
    r = fl.iloc[0]

    def g(k):
        try:
            v = float(r.get(k))
            return v if not math.isnan(v) else 0.0
        except Exception:
            return 0.0

    xb, xs = g("active_buy_x_large"), g("active_sell_x_large")
    lb, ls = g("active_buy_large"), g("active_sell_large")
    pxb, pxs = g("passive_buy_x_large"), g("passive_sell_x_large")
    plb, pls = g("passive_buy_large"), g("passive_sell_large")
    mb, ms = g("active_buy_medium"), g("active_sell_medium")
    pmb, pms = g("passive_buy_medium"), g("passive_sell_medium")
    turnover = g("turnover") or (quote.get("金额") or 0)
    res["主力净额"] = _f((xb + lb) - (xs + ls), 0)          # 同花顺口径：主动特大+主动大单
    res["超大单净额"] = _f((xb - xs) + (pxb - pxs), 0)       # 特大单（主动+被动）
    res["大单净额"] = _f((lb - ls) + (plb - pls), 0)         # 大单（主动+被动）
    res["中单净额"] = _f((mb - ms) + (pmb - pms), 0)
    res["主力净占比%"] = _f(res["主力净额"] / turnover * 100) if turnover else None
    res["特大单买入额"] = _f(xb + pxb, 0)
    res["特大单卖出额"] = _f(xs + pxs, 0)
    res["大单买入额"] = _f(lb + plb, 0)
    res["大单卖出额"] = _f(ls + pls, 0)
    res["成交额"] = _f(turnover, 0)
    w = ctx.get("wencai") or {}
    if w.get("主力净额") is not None:
        res["主力净额(问财)"] = _f(w["主力净额"], 0)
    if w.get("超大单净额") is not None:
        res["超大单净额(问财)"] = _f(w["超大单净额"], 0)
    if w.get("大单净量") is not None:
        res["大单净量%"] = _f(w["大单净量"], 3)
    if w.get("分时ddx") is not None:
        res["分时DDX"] = _f(w["分时ddx"], 2)
    return res


def build_tape(ctx):
    """逐笔成交：分时成交明细 + 大单统计 + 分时博弈。"""
    tk = ctx.get("ticks")
    res = {"最近成交": [], "大单": [], "分时博弈": None, "主动买额": None,
           "主动卖额": None, "大单净额(逐笔)": None}
    if tk is None or len(tk) == 0:
        return res

    df = tk.copy()

    def ts(t):
        try:
            return datetime.fromtimestamp(int(t), TZ_CN).strftime("%H:%M:%S")
        except Exception:
            return str(t)

    prev = None
    buy_amt = sell_amt = 0.0
    n_cls = 0
    big = []
    for _, r in df.iterrows():
        p = float(r.get("price") or 0)
        v = float(r.get("volume") or 0)
        amt = p * v
        dt = r.get("deal_type")
        if dt == 1:
            side = "买"
        elif dt == 2:
            side = "卖"
        elif prev is not None and p > prev:
            side = "买"
        elif prev is not None and p < prev:
            side = "卖"
        else:
            side = "中性"
        prev = p
        if side == "买":
            buy_amt += amt
            n_cls += 1
        elif side == "卖":
            sell_amt += amt
            n_cls += 1
        rec = {"时间": ts(r.get("time")), "价格": _px(p), "量": _f(v, 0),
               "方向": side, "金额": _f(amt, 0)}
        res["最近成交"].append(rec)
        if amt >= 1_000_000:
            big.append(rec)
    res["最近成交"] = res["最近成交"][-20:]
    res["大单"] = big[-10:]
    tot = buy_amt + sell_amt
    res["逐笔抽样笔数"] = n_cls
    res["主动买额(抽样)"] = _f(buy_amt, 0)
    res["主动卖额(抽样)"] = _f(sell_amt, 0)
    res["逐笔净额(抽样)"] = _f(buy_amt - sell_amt, 0)
    res["分时博弈(逐笔抽样)"] = _f((buy_amt - sell_amt) / tot * 100) if tot and n_cls >= 30 else None
    return res


def build_price_volume(ctx):
    pvl = ctx.get("pvl")
    res = {"分价TOP": [], "主买占比%": None}
    if pvl is None or len(pvl) == 0:
        return res
    df = pvl.copy()
    df["金额估算"] = df["price"].astype(float) * df["volume"].astype(float)
    top = df.sort_values("volume", ascending=False).head(10)
    for _, r in top.iterrows():
        res["分价TOP"].append({
            "价格": _px(r.get("price")),
            "量": _f(r.get("volume"), 0),
            "主买": _f(r.get("in_volume"), 0),
            "主卖": _f(r.get("out_volume"), 0),
        })
    iv = pd.to_numeric(df["in_volume"], errors="coerce").fillna(0).sum()
    ov = pd.to_numeric(df["out_volume"], errors="coerce").fillna(0).sum()
    if iv + ov:
        res["主买占比%"] = _f(iv / (iv + ov) * 100)
        res["主卖占比%"] = _f(ov / (iv + ov) * 100)
        res["分时博弈(全天)"] = _f(res["主买占比%"] - res["主卖占比%"])
    return res


def _series(ctx):
    """供网页画图用的序列：日/周/月收盘 + 分时价格与均价。"""
    out = {}

    def pack(df, n):
        if df is None or len(df) == 0:
            return None
        d = df.tail(n)
        try:
            labs = [str(i)[:10] for i in d.index]
        except Exception:
            labs = [str(i) for i in range(len(d))]
        return {"labels": labs, "closes": [_px(c) for c in d["close"].tolist()]}

    for k, src, n in (("日", "day", 20), ("周", "week", 12), ("月", "month", 6)):
        try:
            out[k] = pack(ctx.get(src), n)
        except Exception:
            out[k] = None

    idf = ctx.get("intraday")
    if idf is not None and len(idf):
        try:
            step = max(1, len(idf) // 240)
            d = idf.iloc[::step]
            vol = d["volume"].tolist()
            turn = d["turnover"].tolist()
            avg = []
            for v, t in zip(vol, turn):
                avg.append(_f(t / v) if v else None)
            out["分时"] = {"price": [_f(p) for p in d["price"].tolist()], "avg": avg}
        except Exception:
            pass
    return out


def _last_day(ctx):
    df = ctx.get("day")
    if df is None or len(df) == 0:
        return None
    try:
        return str(df.index[-1])[:10]
    except Exception:
        return None


def _bar_clock(j):
    """会话内第 j 根（0基）5 分钟K线的收盘时刻：09:30 起算，跨过午休。"""
    end_min = (j + 1) * 5
    if end_min <= 120:
        t = 9 * 60 + 30 + end_min
    else:
        t = 13 * 60 + (end_min - 120)
    return "%02d:%02d" % (t // 60, t % 60)


def build_min5(ctx, n=8):
    df = ctx.get("min5")
    if df is None or len(df) == 0:
        return []
    idf = ctx.get("intraday")
    today_bars = None
    if idf is not None and len(idf) > 1:
        today_bars = max(1, (len(idf) - 1) // 5)
    seg = df.tail(today_bars) if today_bars else df
    rows = []
    total = len(seg)
    for j, (idx, r) in enumerate(seg.tail(n).iterrows()):
        pos = total - min(n, total) + j
        rows.append({"时间": _bar_clock(pos) if today_bars else "T-%d" % (total - 1 - pos),
                     "开": _px(r.get("open")), "高": _px(r.get("high")),
                     "低": _px(r.get("low")), "收": _px(r.get("close")),
                     "量": _f(r.get("volume"), 0), "额": _f(r.get("turnover"), 0)})
    return rows


def build_valuation(ctx):
    w = ctx.get("wencai") or {}
    v = {}
    m = {"市盈率": "市盈率", "市净率": "市净率", "市销率": "市销率", "总市值": "总市值",
         "流通市值": "流通市值", "股息率": "股息率", "总股本": "总股本", "流通股本": "流通股本",
         "实时估值": "实时估值", "最新份额": "最新份额", "跟踪指数": "跟踪指数",
         "折价率": "折价率", "跟踪误差": "跟踪误差", "每股收益": "每股收益",
         "每股净资产": "每股净资产", "净利润": "净利润", "营业收入": "营业收入",
         "净利率": "净利率", "每股公积金": "每股公积金", "今昨成交比": "今昨成交比"}
    for k_src, k_dst in m.items():
        if w.get(k_src) is not None:
            v[k_dst] = w[k_src]

    # 基金 / ETF 专有字段
    if ctx.get("is_fund"):
        for k in ("基金规模", "单位净值", "最新净值", "参考净值", "累计净值",
                  "折溢价率", "基金份额", "场内份额", "跟踪指数代码", "管理费率",
                  "上市日期", "基金成立日", "基金类型"):
            if w.get(k) is not None:
                v[k] = w[k]
        # 折溢价率：优先用基金口径的"折溢价率"，否则回退"折价率"
        if w.get("折溢价率") is not None:
            v["折价率"] = w["折溢价率"]
        # 净值口径兜底：不同基金/日期返回的净值字段不一致，统一补齐
        nv = v.get("参考净值") or v.get("单位净值") or v.get("最新净值")
        if nv is not None:
            v.setdefault("参考净值", nv)
            v.setdefault("单位净值", nv)
        # 规模兜底：份额 × 净值（估算）
        if v.get("基金规模") is None and v.get("基金份额") and nv:
            try:
                v["基金规模"] = float(v["基金份额"]) * float(nv)
                v["基金规模估算"] = True
            except Exception:
                pass
        if v.get("基金规模") and not v.get("总市值"):
            v["总市值"] = v["基金规模"]        # 便于统一口径展示规模
        if v.get("基金份额") and not v.get("总股本"):
            v["总股本"] = v["基金份额"]        # 份额≈股本口径

    # 个股场景下“实时估值”字段返回的是总市值，属噪声，剔除
    try:
        if v.get("实时估值") and v.get("总市值"):
            if abs(float(v["实时估值"]) / float(v["总市值"]) - 1) < 0.001:
                v.pop("实时估值")
    except Exception:
        pass
    fin = ctx.get("fin")
    if fin is not None and len(fin):
        r = fin.iloc[0]
        for k in ("total_shares", "float_shares", "net_asset_per_share"):
            if r.get(k) is not None:
                v[k] = _clean(r.get(k))
    return v


# --------------------------------------------------------------------------
# 预判模型
# --------------------------------------------------------------------------
def _score_day(q, ind, ob, fl, tape, pv):
    s = 0.0
    basis = []

    def add(v, txt):
        nonlocal s
        s += v
        basis.append("%s (%+.0f)" % (txt, v))

    close = q.get("收盘") or q.get("现价")
    ma5, ma10, ma20 = ind.get("MA5"), ind.get("MA10"), ind.get("MA20")
    if close and ma5 and ma10 and ma20:
        if ma5 > ma10 > ma20:
            add(14, "均线多头排列 MA5>MA10>MA20")
        elif ma5 < ma10 < ma20:
            add(-14, "均线空头排列 MA5<MA10<MA20")
        else:
            add(0, "均线交织")
        if close > ma5:
            add(5, "收盘站上MA5")
        else:
            add(-5, "收盘失守MA5")
        if ma20 and close > ma20:
            add(6, "收盘站上MA20")
        else:
            add(-6, "收盘跌破MA20")
    if ind.get("MACD多头"):
        add(8, "MACD DIF在DEA上方")
    else:
        add(-8, "MACD DIF在DEA下方")
    if ind.get("MACD金叉"):
        add(5, "MACD金叉")
    j = ind.get("KDJ-J")
    if j is not None:
        if j > 100:
            add(-6, "KDJ-J超买(>100)")
        elif j < 0:
            add(6, "KDJ-J超卖(<0)")
        else:
            add(2 if ind.get("KDJ金叉") else 0, "KDJ中性/金叉")
    r12 = ind.get("RSI12")
    if r12 is not None:
        if r12 > 75:
            add(-5, "RSI12超买")
        elif r12 < 30:
            add(5, "RSI12超卖")
    lb = q.get("量比")
    if lb is not None:
        if lb >= 1.5:
            add(5, "量比放大(%.2f)" % lb)
        elif lb <= 0.7:
            add(-3, "量比萎缩(%.2f)" % lb)
    zf = q.get("涨跌幅")
    if zf is not None:
        add(max(-6, min(6, zf)), "当日涨跌幅%+.2f%%" % zf)
    if q.get("现价") and q.get("均价"):
        add(4 if q["现价"] > q["均价"] else -4,
            "现价%s均价" % ("高于" if q["现价"] > q["均价"] else "低于"))
    if fl:
        pct = fl.get("主力净占比%")
        if pct is not None:
            add(max(-12, min(12, pct * 1.2)), "主力净占比%+.2f%%" % pct)
    if ob.get("买卖力道") is not None:
        add(max(-6, min(6, ob["买卖力道"] / 10)), "买卖力道%+.1f%%" % ob["买卖力道"])
    gam = pv.get("分时博弈(全天)")
    if gam is None:
        gam = tape.get("分时博弈(逐笔抽样)")
    if gam is not None:
        add(max(-8, min(8, gam / 8)), "分时博弈%+.1f%%" % gam)
    if pv.get("主买占比%") is not None:
        add(max(-6, min(6, (pv["主买占比%"] - 50) / 4)), "分价主买占比%.1f%%" % pv["主买占比%"])
    return max(-100, min(100, s)), basis


def _score_week(ctx, ind, q):
    s = 0.0
    basis = []
    wk = ctx.get("week")
    if wk is not None and len(wk) >= 10:
        c = wk["close"]
        ma5 = c.rolling(5).mean().iloc[-1]
        ma10 = c.rolling(10).mean().iloc[-1]
        cur = c.iloc[-1]
        if cur > ma5 > ma10:
            s += 20
            basis.append("周线站上MA5/MA10且多头 (+20)")
        elif cur < ma5 < ma10:
            s -= 20
            basis.append("周线跌破MA5/MA10且空头 (-20)")
        else:
            basis.append("周线均线交织 (0)")
        d, de, _ = macd(c)
        if d.iloc[-1] > de.iloc[-1]:
            s += 15
            basis.append("周线MACD多头 (+15)")
        else:
            s -= 15
            basis.append("周线MACD空头 (-15)")
        r = rsi(c, 14).iloc[-1]
        if r > 70:
            s -= 10
            basis.append("周线RSI超买 (-10)")
        elif r < 30:
            s += 10
            basis.append("周线RSI超卖 (+10)")
    w = ctx.get("wencai") or {}
    for key, wgt in (("20日涨幅", 0.6), ("60日涨幅", 0.35)):
        v = w.get(key)
        if v is not None:
            add = max(-20, min(20, float(v) * wgt))
            s += add
            basis.append("%s %+.2f%% (%+.0f)" % (key, float(v), add))
    if q.get("量比") is not None and q["量比"] > 1.2:
        s += 5
        basis.append("量能配合 (+5)")
    return max(-100, min(100, s)), basis


def _score_month(ctx, ind, q):
    s = 0.0
    basis = []
    mo = ctx.get("month")
    if mo is not None and len(mo) >= 8:
        c = mo["close"]
        ma3 = c.rolling(3).mean().iloc[-1]
        ma6 = c.rolling(6).mean().iloc[-1]
        cur = c.iloc[-1]
        if cur > ma3 > ma6:
            s += 25
            basis.append("月线多头排列 (+25)")
        elif cur < ma3 < ma6:
            s -= 25
            basis.append("月线空头排列 (-25)")
        else:
            basis.append("月线均线交织 (0)")
        d, de, _ = macd(c)
        if d.iloc[-1] > de.iloc[-1]:
            s += 15
            basis.append("月线MACD多头 (+15)")
        else:
            s -= 15
            basis.append("月线MACD空头 (-15)")
    w = ctx.get("wencai") or {}
    ytd = w.get("年初至今涨幅")
    if ytd is not None:
        a = max(-20, min(20, float(ytd) * 0.3))
        s += a
        basis.append("年初至今 %+.2f%% (%+.0f)" % (float(ytd), a))
    pe, pb = w.get("市盈率"), w.get("市净率")
    if pe is not None:
        if 0 < float(pe) < 15:
            s += 8
            basis.append("PE偏低 %.1f (+8)" % float(pe))
        elif float(pe) > 60:
            s -= 8
            basis.append("PE偏高 %.1f (-8)" % float(pe))
    if pb is not None and float(pb) > 8:
        s -= 5
        basis.append("PB偏高 %.1f (-5)" % float(pb))
    dy = w.get("股息率")
    if dy is not None and float(dy) > 2:
        s += 5
        basis.append("股息率 %.2f%% (+5)" % float(dy))
    return max(-100, min(100, s)), basis


def _forecast(name, score, atr_pct, close, price_limit_up=10.0):
    """按评分与波动率给出涨跌幅预判区间（仅统计口径，非投资建议）。"""
    amp = atr_pct if atr_pct and atr_pct > 0 else 1.5
    cfg = {
        "日": (0.65, 0.85),
        "周": (1.20, 1.80),
        "月": (2.10, 3.20),
    }[name]
    center = score / 100.0 * cfg[0] * amp
    band = cfg[1] * amp
    low, high = center - band, center + band
    cap = {"日": price_limit_up * 0.95, "周": price_limit_up * 3, "月": price_limit_up * 8}[name]
    low, high = max(low, -cap), min(high, cap)
    if score >= 30:
        direction = "偏强"
    elif score <= -30:
        direction = "偏弱"
    else:
        direction = "震荡"
    return {
        "涨跌幅区间%": [_f(low), _f(high)],
        "中枢%": _f(center),
        "方向": direction,
        "评分": _f(score, 0),
        "目标价区间": [_px(close * (1 + low / 100)), _px(close * (1 + high / 100))] if close else None,
        "波动率(ATR%)": _f(amp),
    }


def build_forecast(ctx, q, ind):
    close = q.get("收盘") or q.get("现价")
    atr_pct = ind.get("ATR14%") or 2.0
    # 涨跌幅限制：由涨停价/昨收反推（主板 10%、科创/创业板 20%、部分 ETF 20%）
    limit = 10.0
    try:
        up, pc = q.get("涨停价"), q.get("昨收")
        if up and pc:
            lim = round((float(up) / float(pc) - 1) * 100, 1)
            if 1.0 <= lim <= 30.0:
                limit = lim
    except Exception:
        pass
    # 兜底：创业板/科创板相关标的按 20% 限制（含跟踪创业板指/科创50的双创 ETF）
    if ctx.get("is_fund") and limit == 10.0:
        nm = (ctx.get("name") or "") + str((ctx.get("wencai") or {}).get("跟踪指数") or "")
        if ("创业板" in nm) or ("科创" in nm) or ("双创" in nm):
            limit = 20.0
    ctx["price_limit"] = limit
    fc = {}
    for name, fn in (("日", _score_day), ("周", _score_week), ("月", _score_month)):
        try:
            if name == "日":
                score, basis = _score_day(q, ind, ctx.get("_ob", {}), ctx.get("_fl", {}),
                                          ctx.get("_tape", {}), ctx.get("_pv", {}))
            else:
                score, basis = fn(ctx, ind, q)
        except Exception as exc:
            score, basis = 0.0, ["评分计算异常: %s" % str(exc)[:60]]
        item = _forecast(name, score, atr_pct, close, price_limit_up=limit)
        item["依据"] = basis
        fc[name] = item

    # 趋势线（日/周/月）
    tl = {}
    day = ctx.get("day")
    if day is not None and len(day) >= 25:
        tl["日"] = trendline(day["close"].iloc[-20:].tolist())
    wk = ctx.get("week")
    if wk is not None and len(wk) >= 15:
        tl["周"] = trendline(wk["close"].iloc[-12:].tolist())
    mo = ctx.get("month")
    if mo is not None and len(mo) >= 9:
        tl["月"] = trendline(mo["close"].iloc[-6:].tolist())
    return fc, tl


# --------------------------------------------------------------------------
# 走势展望（今日 / 明日 / 下周 / 本周 / 本月 / 下月）
# --------------------------------------------------------------------------
# ---- 历法（干支/五行）：玄学维度解读，民俗口径，仅供研判参考 ----
_GAN = "甲乙丙丁戊己庚辛壬癸"
_ZHI = "子丑寅卯辰巳午未申酉戌亥"
_SX = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
_WX_GAN = {"甲": "木", "乙": "木", "丙": "火", "丁": "火", "戊": "土",
           "己": "土", "庚": "金", "辛": "金", "壬": "水", "癸": "水"}
_WX_ZHI = {"子": "水", "丑": "土", "寅": "木", "卯": "木", "辰": "土", "巳": "火",
           "午": "火", "未": "土", "申": "金", "酉": "金", "戌": "土", "亥": "水"}
_WX_SECTOR = {
    "金": "金融权重（银行/保险）与有色",
    "木": "成长赛道（医药/消费/农业）",
    "水": "流动性敏感（券商/两融/基建）",
    "火": "科技题材（AI算力/电子/军工）",
    "土": "地产基建与资源周期",
}
_WX_SHENG = {"木": "火", "火": "土", "土": "金", "金": "水", "水": "木"}
_WX_KE = {"木": "土", "土": "水", "水": "火", "火": "金", "金": "木"}
_MONTH_ZHI = {1: "丑", 2: "寅", 3: "卯", 4: "辰", 5: "巳", 6: "午",
              7: "未", 8: "申", 9: "酉", 10: "戌", 11: "亥", 12: "子"}


def _jdn(d):
    a = (14 - d.month) // 12
    y = d.year + 4800 - a
    m = d.month + 12 * a - 3
    return d.day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def _year_ganzhi(d):
    """年柱（立春近似换年）。"""
    y = d.year - 1 if (d.month, d.day) < (2, 4) else d.year
    idx = (y - 4) % 60
    return _GAN[idx % 10] + _ZHI[idx % 12], y


def shengxiao(d):
    return _SX[(_year_ganzhi(d)[1] - 4) % 12]


def ganzhi_day(d):
    idx = (_jdn(d) + 49) % 60
    return _GAN[idx % 10] + _ZHI[idx % 12]


def ganzhi_month(d):
    """月柱（月支按公历月近似，未精确按节气换月）。"""
    zhi = _MONTH_ZHI[d.month]
    order = (d.month - 2) % 12
    yg = _GAN.index(_year_ganzhi(d)[0][0])
    return _GAN[((yg % 5) * 2 + 2 + order) % 10] + zhi


def _wx_state(wx, mwx):
    if wx == mwx:
        return "同气当值"
    if _WX_SHENG[mwx] == wx:
        return "得月令相生"
    if _WX_SHENG[wx] == mwx:
        return "泄气于月令"
    if _WX_KE[mwx] == wx:
        return "受月令所克"
    return "克月令而先强后滞"


def _season_cn(d):
    m = d.month
    if m in (2, 3, 4):
        return "春木生发"
    if m in (5, 6, 7):
        return "夏火当旺"
    if m in (8, 9, 10):
        return "秋金肃杀"
    return "冬水收藏"


def _amt(v):
    try:
        v = float(v)
    except Exception:
        return None
    if math.isnan(v):
        return None
    if abs(v) >= 1e8:
        return "%.2f亿" % (v / 1e8)
    if abs(v) >= 1e4:
        return "%.2f万" % (v / 1e4)
    return "%.0f" % v


# ---- 国际行情（新浪财经公开接口）：美股/港股/大宗/汇率 ----
_INTL_CODES = [("道琼斯", "int_dji"), ("纳斯达克", "int_nasdaq"), ("标普500", "int_sp500"),
               ("恒生指数", "int_hangseng"), ("纽约原油", "hf_CL"), ("纽约黄金", "hf_GC"),
               ("离岸人民币", "fx_susdcnh")]
_INTL_CACHE = {"t": 0.0, "data": None}


def fetch_intl(timeout=8):
    """抓取国际行情（缓存 3 分钟）；失败返回 None。"""
    now_t = time.time()
    if _INTL_CACHE["data"] is not None and now_t - _INTL_CACHE["t"] < 180:
        return _INTL_CACHE["data"]
    import urllib.request
    url = "https://hq.sinajs.cn/list=" + ",".join(c for _, c in _INTL_CODES)
    req = urllib.request.Request(url, headers={
        "Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"})
    txt = urllib.request.urlopen(req, timeout=timeout).read().decode("gbk", "ignore")
    raw = {}
    for line in txt.splitlines():
        if 'hq_str_' not in line or '="' not in line:
            continue
        raw[line.split("hq_str_")[1].split("=")[0]] = line.split('="')[1].rstrip('";')
    res = {}
    for name, code in _INTL_CODES:
        seg = raw.get(code) or ""
        if not seg:
            continue
        p = seg.split(",")
        try:
            if code.startswith("int_"):
                res[name] = {"最新": round(float(p[1]), 2), "涨跌幅%": round(float(p[3]), 2)}
            elif code.startswith("hf_"):
                cur, prev = float(p[0]), float(p[7])
                res[name] = {"最新": round(cur, 2),
                             "涨跌幅%": round((cur / prev - 1) * 100, 2) if prev else None}
            elif code.startswith("fx_s"):
                res[name] = {"最新": round(float(p[8]), 4), "涨跌幅%": round(float(p[10]), 2)}
        except Exception:
            continue
    _INTL_CACHE.update(t=now_t, data=(res or None))
    return res or None


def _intl_text(intl, span):
    if not intl:
        return "外盘行情未取到，国际面暂缺（可稍后刷新重试）"
    def g(k):
        return (intl.get(k) or {}).get("涨跌幅%")
    parts = []
    us = [("道指", g("道琼斯")), ("纳指", g("纳斯达克")), ("标普", g("标普500"))]
    txt = "、".join("%s %+.2f%%" % (n, v) for n, v in us if v is not None)
    if txt:
        parts.append("隔夜美股 " + txt)
    if g("恒生指数") is not None:
        parts.append("港股恒指 %+.2f%%" % g("恒生指数"))
    if g("纽约原油") is not None:
        parts.append("原油 %+.2f%%" % g("纽约原油"))
    if g("纽约黄金") is not None:
        parts.append("黄金 %+.2f%%" % g("纽约黄金"))
    cnh = intl.get("离岸人民币") or {}
    if cnh.get("涨跌幅%") is not None:
        c = cnh["涨跌幅%"]
        parts.append("USDCNH %.4f（人民币%s%.2f%%）" % (cnh.get("最新") or 0, "升值" if c < 0 else "贬值", abs(c)))
    base = "；".join(parts)
    dj, hk = g("道琼斯"), g("恒生指数")
    if span == "long":
        judge = "中期看外盘方向与人民币汇率：外盘走强叠加人民币升值利于外资回流，反之压制权重与出口链"
    elif dj is not None and hk is not None:
        if dj > 0 and hk > 0:
            judge = "外盘普涨、风险偏好回升，利于 A 股高开修复"
        elif dj < 0 and hk < 0:
            judge = "外盘普跌、避险升温，A 股易低开承压"
        else:
            judge = "外盘分化，A 股更易走独立行情"
    else:
        judge = "外盘信号不全，定价以国内量能与资金为主"
    return (base + "；" + judge) if base else judge


def _a_share_rules(period, dates, ctx, q):
    """A 股日历规律（历史统计口径）。"""
    tips = []
    wds = {d.weekday() for d in dates}
    if period in ("week", "next_week"):
        if 0 in wds:
            tips.append("周一常一次性消化周末消息，跳空后多空切换快")
        if 4 in wds:
            tips.append("周五资金倾向减仓避险，尾盘易走弱")
    if any(d.day >= 25 for d in dates):
        tips.append("跨月末，资金面偏紧、机构调仓频繁，波动放大")
    if any((d.month in (3, 6, 9, 12) and d.day >= 20) for d in dates):
        tips.append("季末考核窗口，权重护盘与调仓并存")
    if any((d.month == 9 and d.day >= 22) for d in dates):
        tips.append("国庆长假临近：长假前 5 个交易日历史多为缩量避险")
    if period == "next_month" and dates and dates[0].month == 10:
        tips.append("长假后首周：首日高开概率大，但高开低走亦不罕见，宜等量能确认")
    if any(d.month in (4, 10) for d in dates):
        tips.append("业绩披露窗口，业绩雷与预增股分化明显")
    if any((d.month == 12 and d.day >= 10) for d in dates):
        tips.append("年末排名与资金考核期，机构行为主导")
    if not tips:
        tips.append("区间内无显著日历效应，节奏由量能与资金主导")
    return "；".join(tips[:3])


def _renhe(ctx, q):
    def num(v):
        try:
            f = float(v)
            return None if math.isnan(f) else f
        except Exception:
            return None
    qb, tr, chg = num(q.get("量比")), num(q.get("换手率")), num(q.get("涨跌幅"))
    main = num((ctx.get("_fl") or {}).get("主力净额"))
    bits = []
    if qb is not None:
        bits.append("量比 %.2f" % qb)
    if tr is not None:
        bits.append("换手 %.2f%%" % tr)
    if main:
        bits.append("主力净额 %s" % _amt(main))
    if qb is not None and qb > 1.2:
        mood = "人气聚集、量价配合" if (chg or 0) > 0 else "放量走弱、人气涣散"
    elif qb is not None and qb < 0.8:
        mood = "量能清淡、存量博弈"
    else:
        mood = "人气中性、多空分歧"
    return ("；".join(bits) + "，" + mood) if bits else mood


_DIM_WEIGHTS = {
    "today":      {"天时": 15, "地利": 10, "人和": 25, "国际": 30, "规律": 20},
    "tomorrow":   {"天时": 15, "地利": 10, "人和": 25, "国际": 30, "规律": 20},
    "week":       {"天时": 15, "地利": 15, "人和": 20, "国际": 25, "规律": 25},
    "next_week":  {"天时": 15, "地利": 15, "人和": 20, "国际": 25, "规律": 25},
    "month":      {"天时": 15, "地利": 20, "人和": 15, "国际": 20, "规律": 30},
    "next_month": {"天时": 15, "地利": 20, "人和": 15, "国际": 20, "规律": 30},
}
_DIM_WEIGHT_NOTE = "权重=各维度在综合研判中的相对重要性（合计 100%），非收益贡献度；越近的周期越看资金（人和）与外盘（国际），越远的周期越看趋势规律与月令位置"


def _dimensions(period, anchor, dates, ctx, q, ind, intl):
    """三维解读：玄学（天时/地利/人和）、国际趋势、A股市场规律，并附各维度权重占比。"""
    dim = {}
    try:
        gz_d, gz_m = ganzhi_day(anchor), ganzhi_month(anchor)
        wx_d, wx_m = _WX_GAN[gz_d[0]], _WX_ZHI[gz_m[1]]
        state = _wx_state(wx_d, wx_m)
        win = wx_d if ("同气" in state or "相生" in state) else wx_m
        dim["玄学"] = {
            "天时": "%s日柱（%s气%s）：%s得气，%s受制" % (gz_d, wx_d, state,
                                                _WX_SECTOR[win], _WX_SECTOR[_WX_KE[win]]),
            "地利": "%s月令%s当令（%s）：%s占位，%s失位" % (gz_m, wx_m, _season_cn(anchor),
                                                 _WX_SECTOR[wx_m], _WX_SECTOR[_WX_KE[wx_m]]),
            "人和": _renhe(ctx, q),
        }
    except Exception:
        dim["玄学"] = {}
    dim["国际趋势"] = _intl_text(intl, "long" if period in ("month", "next_month") else "short")
    dim["A股规律"] = _a_share_rules(period, dates, ctx, q)
    dim["权重%"] = dict(_DIM_WEIGHTS.get(period, _DIM_WEIGHTS["week"]))
    dim["权重说明"] = _DIM_WEIGHT_NOTE
    return dim


# ---- 交易日与区间外推 ----
def _month_end(d):
    nxt = d.replace(year=d.year + 1, month=1, day=1) if d.month == 12 else d.replace(month=d.month + 1, day=1)
    return nxt - timedelta(days=1)


def _trade_dates(a, b):
    out, cur = [], a
    while cur <= b:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _day_maps(ctx):
    daydf = ctx.get("day")
    rows, ohlc = [], {}
    if daydf is not None and len(daydf):
        cols = set(getattr(daydf, "columns", []))
        for idx in daydf.index:
            s = str(idx)[:10]
            try:
                d0 = datetime.strptime(s, "%Y-%m-%d").date()
                c0 = float(daydf.loc[idx, "close"])
            except Exception:
                continue
            rows.append((d0, c0))
            try:
                o0 = float(daydf.loc[idx, "open"]) if "open" in cols else None
                h0 = float(daydf.loc[idx, "high"]) if "high" in cols else None
                l0 = float(daydf.loc[idx, "low"]) if "low" in cols else None
                if o0 is not None and h0 is not None and l0 is not None:
                    ohlc[d0] = (o0, h0, l0, c0)
            except Exception:
                pass
    rows.sort()
    prev_close = {}
    for i, (d0, c0) in enumerate(rows):
        if i:
            prev_close[d0] = rows[i - 1][1]
    return rows, {d0: c0 for d0, c0 in rows}, prev_close, ohlc


def _realized_drift(dates, actual_map, base_d, atr, k=5, active=None, cap_ratio=0.55):
    """区间内已实现动量：最近 k 个实际交易日（含今日盘中预收）的日均涨跌幅（%），按 0.55×ATR 限幅。

    返回 (日均涨跌幅%, 参与计算的实际交易日数)；实际点不足 2 个时返回 (None, n)。
    """
    pts = [actual_map.get(d0) for d0 in dates if (d0 in actual_map and d0 <= base_d)]
    if active and active[1] and active[0] == base_d and base_d not in actual_map:
        pts.append(active[1])
    pts = [x for x in pts if x]
    if len(pts) < 2:
        return None, len(pts)
    seg = pts[-(k + 1):] if len(pts) > (k + 1) else pts
    pcts = [((b / a) - 1) * 100.0 for a, b in zip(seg, seg[1:]) if a]
    if not pcts:
        return None, len(pts)
    m = sum(pcts) / len(pcts)
    lim = max(cap_ratio * (atr or 2.0), 0.05)
    return max(-lim, min(lim, m)), len(pcts)


def _blend_drift(score_drift, mom, w):
    """把「评分中枢折算的日斜率」与「区间已实现日动量」加权合成外推斜率；无动量时退化为纯评分斜率。"""
    if mom is None:
        return score_drift
    return w * mom + (1.0 - w) * score_drift


def _zigzags(open_p, high_p, low_p, close_p, atr, predicted, pp=8):
    """把单日 开/高/低/收 展开为 pp 个确定性采样点（非随机），走势为 冲高→回落→探低→收 的日内节奏。"""
    o = open_p if open_p else close_p
    h = high_p if high_p else max(o, close_p)
    l = low_p if low_p else min(o, close_p)
    c = close_p
    try:
        h = max(h, o, c)
        l = min(l, o, c)
    except TypeError:
        h, l = max(o, c), min(o, c)
    pts = []
    ih = max(1, int(round((pp - 1) * 0.375)))          # 冲高采样点
    il = max(ih + 1, int(round((pp - 1) * 0.75)))      # 探低采样点
    il = min(il, pp - 2)
    for k in range(pp):
        if k <= ih:
            u = k / float(ih)
            v = o + (h - o) * u
        elif k <= il:
            u = (k - ih) / float(il - ih)
            v = h + (l - h) * u
        else:
            u = (k - il) / float(pp - 1 - il)
            v = l + (c - l) * u
        if predicted:
            swing = (abs(c) or 1.0) * (atr / 100.0) * 0.18
            v += swing * math.sin(k * 1.5708 + (l or 0.0))
        pts.append(_px(v))
    pts[0] = _px(o)
    pts[ih] = _px(h)
    pts[il] = _px(l)
    pts[-1] = _px(c)
    return pts


def _ohlc_path(o0, h0, l0, c0, pp=8):
    """实际交易日：用真实 OHLC 展开成 pp 点日内路径。"""
    return _zigzags(o0, h0, l0, c0, 0.0, False, pp)


def _curve_pts(open_p, close_p, atr, n=8, up=None):
    """确定性日内曲线（开→收 + sin 包络 + 微幅波动，非随机）。

    近端预测链与各周期卡片共用本函数：明日卡取 n=241，周期卡对同一曲线取 n=8 采样，
    因此同一交易日在不同卡片中的曲线形状与首尾取值完全一致（曲线只有一个来源）。
    """
    o = open_p if open_p else close_p
    c = close_p
    n = max(2, int(n))
    bulge = 1.0 if (up if up is not None else (c if c is not None else o) >= o) else -1.0
    pts = []
    for j in range(n):
        t = j / float(n - 1)
        v = o + (c - o) * t
        if 0 < t < 1:
            v *= (1 + bulge * 0.35 * (atr or 0.0) / 100.0 * math.sin(math.pi * t))
            v *= (1 + (atr or 0.0) / 100.0 * 0.10 * math.sin(4 * math.pi * t) * bulge)
        pts.append(_px(v))
    pts[0], pts[-1] = _px(o), _px(c)
    return pts


def _day_path(o0, c0, ref, atr, predicted, pp=8):
    """预测/进行中单日：按 开/预收 ± ATR 生成 pp 点日内路径（确定性，非随机）。"""
    open_p = o0 if o0 else (ref if ref else c0)
    band = 0.55 if predicted else 0.35
    swing = (abs(c0) or 1.0) * (atr / 100.0) * band
    h0 = max(open_p, c0) + swing
    l0 = min(open_p, c0) - swing
    return _zigzags(open_p, h0, l0, c0, atr, predicted, pp)


def _build_period(dates, base_d, start_price, drift_pct, atr, actual_map, prev_close_map,
                  active_day=None, active_close=None, ohlc_map=None, active_open=None, seed_map=None):
    """区间每日收盘 + 每日 8 点日内路径：已过交易日取实际（OHLC），今日取盘中预收，未来按 drift 外推。

    外推口径：以前一交易日为基准按 drift_pct 逐日复利；对非末位预测日叠加振幅 0.12×ATR 的
    确定性正弦（相位由日期绑定，非随机），使预测段呈波动节奏而非一条直线；末位预测日回归
    趋势值，保证「期末」等于趋势终值。

    seed_map：近端预测链（{日期: {开盘/最高/最低/收盘/路径}}）。命中时该日直接取链上数值与
    8 点采样路径，不再按 drift 外推——保证同一交易日在「明日 / 本周剩余 / 下周 / 本月」各卡
    中取同一套开高低收与同一形状的曲线。
    """
    PP = 8
    seed_map = seed_map or {}
    days, last, lo, hi, path_flat = [], None, None, None, []
    ohlc_map = ohlc_map or {}
    pred_dates = [d0 for d0 in dates if d0 > base_d and d0 not in actual_map]
    last_pred = pred_dates[-1] if pred_dates else None
    for d0 in dates:
        item = {"日期": d0.strftime("%m-%d"), "星期": _weekday_cn(d0)}
        ref = prev_close_map.get(d0) or last or start_price
        state = None
        seed = seed_map.get(d0) if (d0 > base_d and d0 not in actual_map) else None
        if active_day is not None and d0 == active_day and d0 not in actual_map:
            val, state = active_close, "进行中"
        elif d0 in actual_map:
            val, state = actual_map[d0], "实际"
        elif seed is not None:
            val, state = seed.get("收盘"), "预测"
        elif d0 > base_d:
            val = (last if last else start_price) * (1 + drift_pct / 100.0)
            state = "预测"
            if d0 != last_pred:
                amp = 0.12 * (atr or 0.0) / 100.0
                ph = (d0.toordinal() % 7) / 7.0 * 2 * math.pi
                val *= (1.0 + amp * math.sin(1.7 * (d0.toordinal() % 11) + ph))
        else:
            val = None
        item["状态"] = state or "无数据"
        if val:
            item["收盘"] = _px(val)
            item["涨跌幅%"] = _f((val / ref - 1) * 100) if ref else None
            if state == "实际" and d0 in ohlc_map:
                o0, h0, l0, c0 = ohlc_map[d0]
                item["开盘"], item["最高"], item["最低"] = _px(o0), _px(h0), _px(l0)
                path = _ohlc_path(o0, h0, l0, c0, PP)
            elif seed is not None and seed.get("路径"):
                path = [_px(x) for x in seed["路径"]]
                item["开盘"] = _px(seed.get("开盘") or (ref or val))
                item["最高"] = _px(seed["最高"]) if seed.get("最高") else max(path)
                item["最低"] = _px(seed["最低"]) if seed.get("最低") else min(path)
                item["共用"] = seed.get("来源") or "近端预测链"
            else:
                op0 = active_open if (state == "进行中" and active_open) else (ref or val)
                path = _day_path(op0, val, ref, atr, state == "预测", PP)
                item["开盘"] = _px(op0)
                item["最高"] = max(path)
                item["最低"] = min(path)
            last = val
            lo = val if lo is None else min(lo, val)
            hi = val if hi is None else max(hi, val)
            path_flat.extend(_px(x) for x in path)
        else:
            item["收盘"] = item["涨跌幅%"] = None
            item["开盘"] = item["最高"] = item["最低"] = None
            path_flat.extend([None] * PP)
        days.append(item)
    ticks = [[i * PP, days[i]["日期"]] for i in range(len(days))]
    return {
        "每日": days,
        "路径": path_flat,
        "每日点数": PP,
        "X刻度": ticks,
        "区间低": _px(lo), "区间高": _px(hi), "期末": _px(last),
        "区间涨跌幅%": _f((last / start_price - 1) * 100) if (last and start_price) else None,
    }


def _weekday_cn(d):
    return "周" + "一二三四五六日"[d.weekday()]


def _next_trade_day(d):
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def _prev_trade_day(d):
    n = d - timedelta(days=1)
    while n.weekday() >= 5:
        n -= timedelta(days=1)
    return n


def _bar_clock(j):
    """1 分钟级分时第 j 点（0 基）对应的时刻标签（09:30~11:30 / 13:01~15:00）。"""
    if j <= 120:
        t = 9 * 60 + 30 + j
    else:
        t = 13 * 60 + (j - 120)
    return "%02d:%02d" % (t // 60, t % 60)


def build_outlook(ctx, q, ind, fc):
    """今日、明日、下周、本周、本月、下月的量化走势预测。

    口径：以日线/周线/月线评分中枢 + ATR 外推，统计口径，非投资建议；
    各周期附「玄学（天时/地利/人和）、国际趋势、A股市场规律」三维解读。
    """
    out = {"口径": "按评分中枢与 ATR 外推的统计口径，非投资建议"}
    try:
        close = q.get("收盘") or q.get("现价")
        prev = q.get("昨收")
        op = q.get("今开")
        if not close:
            return out
        atr = float(ind.get("ATR14%") or 2.0)
        day_fc = fc.get("日") or {}
        wk_fc = fc.get("周") or {}
        center = float(day_fc.get("中枢%") or 0.0)
        direction = day_fc.get("方向")
        limit = float(ctx.get("price_limit") or 10.0)
        now = now_cn()
        day_str = _last_day(ctx) or now.strftime("%Y-%m-%d")
        idf = ctx.get("intraday")
        path, n_now = [], 0
        if idf is not None and len(idf):
            try:
                path = [round(float(x), 3) for x in idf["price"].tolist()]
                n_now = len(path)
            except Exception:
                path, n_now = [], 0
        total = 241
        is_today = (day_str == now.strftime("%Y-%m-%d"))
        closed = (not is_today) or (n_now >= total) or (now.strftime("%H:%M") >= "15:00")

        # ---------------- 今日 ----------------
        today = {
            "日期": day_str,
            "状态": "已收盘" if closed else "盘中",
            "已走点数": n_now, "总点数": total,
            "时间轴": [_bar_clock(0), _bar_clock(max(0, n_now - 1)), "15:00"],
            "口径": "实线=分时实际走势；虚线=剩余时段按日线评分中枢外推",
        }
        rest_n = 0 if closed else max(0, total - n_now)
        if closed:
            pc = close
            hi = q.get("最高") or (max(path) if path else close)
            lo = q.get("最低") or (min(path) if path else close)
            today["实际"] = [_px(x) for x in path]
            today["预测"] = []
        else:
            rest = max(0.0, 1.0 - n_now / float(total))
            pc = close * (1 + center / 100.0 * rest * 0.9)
            pc = min(max(pc, close * (1 - limit / 100.0)), close * (1 + limit / 100.0))
            recent = [x for x in ([q.get("最高"), q.get("最低"), op] + (path[-20:] if path else [])) if x]
            hi = max([close, pc] + recent) * (1 + 0.25 * atr / 100.0)
            lo = min([close, pc] + recent) * (1 - 0.25 * atr / 100.0)
            seg = []
            for k in range(rest_n):
                t = (k + 1) / float(rest_n)
                v = close + (pc - close) * t
                v *= (1 + (atr / 100.0) * 0.18 * math.sin(math.pi * t) * (1.0 if center >= 0 else -1.0))
                seg.append(_px(v))
            today["实际"] = [_px(x) for x in path] + [None] * rest_n
            today["预测"] = [None] * max(0, n_now - 1) + [_px(close)] + seg
        today["已走占比%"] = _f((n_now / float(total) * 100.0) if n_now else 0.0, 1)
        today["今开"] = _px(op)
        today["预测收盘"] = _px(pc)
        today["预测最高"] = _px(hi)
        today["预测最低"] = _px(lo)
        today["预测涨跌幅%"] = _f((pc / prev - 1) * 100) if prev else None
        today["区间%"] = [_f((lo / prev - 1) * 100), _f((hi / prev - 1) * 100)] if prev else None
        today["方向"] = direction
        out["today"] = today

        # ---------------- 明日 ----------------
        try:
            base_d = datetime.strptime(day_str, "%Y-%m-%d").date()
        except Exception:
            base_d = now.date()
        nd = _next_trade_day(base_d)
        cap = limit * 0.95
        c1 = max(-cap, min(cap, center * 0.85))
        b1 = 0.85 * atr
        t_lo_pct = max(-cap, c1 - b1)
        t_hi_pct = min(cap, c1 + b1)
        gap = max(-0.35 * atr, min(0.35 * atr, center * 0.3))
        base = pc
        t_open = base * (1 + gap / 100.0)
        t_close = base * (1 + c1 / 100.0)
        t_hi = base * (1 + t_hi_pct / 100.0)
        t_lo = base * (1 + t_lo_pct / 100.0)
        amp = 0.35 * atr
        bulge = 1.0 if c1 >= 0 else -1.0
        tn = 241
        # 全天 241 点路径由 _curve_pts 统一生成（与各周期卡片的近端预测链同源）
        t_path = _curve_pts(t_open, t_close, atr, tn, up=(c1 >= 0))
        t_ticks = [[j, _bar_clock(j)] for j in range(0, tn - 1, 30)]
        t_ticks.append([tn - 1, _bar_clock(tn - 1)])
        out["tomorrow"] = {
            "日期": nd.strftime("%Y-%m-%d"), "星期": _weekday_cn(nd),
            "时间轴": [_bar_clock(0), "10:30", "11:30", "13:30", "14:30", _bar_clock(tn - 1)],
            "X刻度": t_ticks,
            "路径": t_path,
            "总点数": tn,
            "预测开盘": _px(t_open), "预测最高": _px(t_hi),
            "预测最低": _px(t_lo), "预测收盘": _px(t_close),
            "中枢%": _f(c1), "涨跌幅区间%": [_f(t_lo_pct), _f(t_hi_pct)],
            "预测涨跌幅%": _f(c1),
            "方向": "偏强" if c1 >= 0.3 * atr else ("偏弱" if c1 <= -0.3 * atr else "震荡"),
            "口径": "以今日预测收盘为基准，按日线评分中枢与 ATR 外推全天 241 点；本日为「近端预测链」首日，"
                    "下列开盘/最高/最低/收盘被「本周剩余 / 下周 / 本月」卡原样复用（曲线取同一路径的 8 点采样）",
        }

        # ---------------- 下周 / 本周 / 本月 / 下月 ----------------
        monday = base_d - timedelta(days=base_d.weekday())
        friday = monday + timedelta(days=4)
        wk_dates = _trade_dates(monday, friday)
        nw_dates = _trade_dates(monday + timedelta(days=7), monday + timedelta(days=11))
        m_first = base_d.replace(day=1)
        m_dates = _trade_dates(m_first, _month_end(m_first))
        nm_first = _month_end(m_first) + timedelta(days=1)
        nm_dates = _trade_dates(nm_first, _month_end(nm_first))
        _rows, actual_all, prev_map, ohlc_all = _day_maps(ctx)
        wk_center = float(wk_fc.get("中枢%") or 0.0)
        mo_center = float((fc.get("月") or {}).get("中枢%") or 0.0)
        try:
            intl = fetch_intl()
        except Exception:
            intl = None
        act_day = None if closed else base_d
        act_close = None if closed else pc
        wk_score = wk_fc.get("评分")
        mo_score = (fc.get("月") or {}).get("评分")
        # 区间已实现动量（含今日盘中预收）：(日均涨跌幅%, 参与计算的实际交易日数)
        mom_act = (base_d, act_close) if act_close else None
        wk_mom, wk_n = _realized_drift(wk_dates, actual_all, base_d, atr, active=mom_act)
        mo_mom, mo_n = _realized_drift(m_dates, actual_all, base_d, atr, active=mom_act)
        wk_w = 0.45 if wk_n >= 2 else (0.25 if wk_n >= 1 else 0.0)
        mo_w = 0.45 if mo_n >= 3 else (0.30 if mo_n >= 2 else (0.20 if mo_n >= 1 else 0.0))

        def _ew(mom, w):
            """有效权重：无已实现动量时权重归零，退化为纯评分中枢外推。"""
            return w if mom is not None else 0.0

        def _pw(mom):
            return "—" if mom is None else _f(mom, 3)

        def _wrap(label, dates, ref, drift, key, note, basis=None, seed=None):
            blk = _build_period(dates, base_d, ref, drift, atr, actual_all, prev_map,
                                active_day=act_day, active_close=act_close,
                                ohlc_map=ohlc_all, active_open=op, seed_map=seed)
            span = drift * max(1, len(dates))
            blk["周期"] = label
            blk["区间"] = "%s ~ %s" % (dates[0].strftime("%m-%d"), dates[-1].strftime("%m-%d"))
            blk["中枢%"] = _f(span)
            blk["方向"] = "偏强" if span >= 0.3 * atr else ("偏弱" if span <= -0.3 * atr else "震荡")
            blk["口径"] = note
            blk["外推斜率%/日"] = _f(drift, 3)
            blk["起点价"] = _px(ref)
            blk["维度"] = _dimensions(key, dates[0], dates, ctx, q, ind, intl)
            if basis:
                blk["依据"] = [x for x in basis if x]
            return blk

        # ---- 各周期外推斜率（先算，供近端预测链与各卡片共用）----
        wk_drift = _blend_drift(wk_center / max(1, len(wk_dates)), wk_mom, _ew(wk_mom, wk_w))
        nw_w = min(wk_w, 0.35)
        nw_drift = _blend_drift(wk_center / max(1, len(nw_dates)), wk_mom, _ew(wk_mom, nw_w))
        mo_base = mo_center / max(1, len(m_dates))
        mo_drift = _blend_drift(mo_base, mo_mom, _ew(mo_mom, mo_w))
        nm_drift = _blend_drift(mo_center * 0.9 / max(1, len(nm_dates)), mo_mom, _ew(mo_mom, 0.30))

        # ---- 近端预测链（唯一数据源）：明日 → 本周剩余 → 下周 ----
        # 同一交易日只在这条链上定一次价：首日直接复用明日卡（日线评分口径）的开高低收与
        # 241 点路径采样，其后各日按所属周期斜率逐日复利；「本周剩余 / 下周 / 本月」卡片命中
        # 链上日期时一律取链上数值，因此不会出现同一日期两条曲线。
        win_dates = [d0 for d0 in _trade_dates(nd, monday + timedelta(days=11))
                     if d0 not in actual_all]
        seed_map = {}
        for _i, _d0 in enumerate(win_dates):
            if _i == 0:
                seed_map[_d0] = {
                    "开盘": t_open, "最高": t_hi, "最低": t_lo, "收盘": t_close,
                    "路径": [t_path[int(round(k * (tn - 1) / 7.0))] for k in range(8)],
                    "来源": "%s 明日卡（日线评分口径）" % _d0.strftime("%m-%d"),
                }
            else:
                _pv = seed_map[win_dates[_i - 1]]["收盘"]
                _dr = wk_drift if _d0 <= friday else nw_drift
                _v = _pv * (1 + _dr / 100.0)
                if _i != len(win_dates) - 1:
                    _amp = 0.12 * (atr or 0.0) / 100.0
                    _ph = (_d0.toordinal() % 7) / 7.0 * 2 * math.pi
                    _v *= (1.0 + _amp * math.sin(1.7 * (_d0.toordinal() % 11) + _ph))
                _seg = _curve_pts(_pv, _v, atr, 8)
                seed_map[_d0] = {
                    "开盘": _pv, "最高": max(_seg), "最低": min(_seg), "收盘": _v,
                    "路径": _seg,
                    "来源": "%s 本周卡（周线评分+本周动量）" % _d0.strftime("%m-%d")
                    if _d0 <= friday else "%s 下周卡（周线评分+本周动量）" % _d0.strftime("%m-%d"),
                }
        _linked = "%s 起（%s）与「明日 / 下周 / 本月」各卡共用同一近端预测链：开盘、最高、最低、收盘与曲线形状逐点一致" % (
            win_dates[0].strftime("%m-%d") if win_dates else "-",
            " / ".join(d0.strftime("%m-%d") for d0 in win_dates[:3]) + (" 等" if len(win_dates) > 3 else ""))

        # 本周：已过交易日实际 + 剩余交易日外推（评分中枢 与 已实现动量 加权）
        wk_rest_n = len([d0 for d0 in wk_dates if d0 > base_d])
        out["week"] = _wrap(
            "本周", wk_dates, prev_map.get(wk_dates[0]) or pc, wk_drift, "week",
            "已过交易日取实际收盘（含今日盘中预收，即实线段）；明日取明日卡口径，其后按「周线评分中枢 + "
            "本周已实现动量」加权斜率逐日外推（与近端预测链一致）；未剔除法定节假日", 
            ["起点：本周首日前收盘 %s ｜ 周线评分 %s → 周中枢 %s%%" %
             (_px(prev_map.get(wk_dates[0]) or pc), wk_score, _f(wk_center)),
             "ATR14 %s%%；本周剩余交易日 %d 天（共 %d 天）" % (_f(atr), wk_rest_n, len(wk_dates)),
             "本周已实现动量 %s%%/日（%d 个实际交易日，权重 %d%%）→ 与评分中枢加权得外推斜率 %s%%/日"
             % (_pw(wk_mom), wk_n, round(_ew(wk_mom, wk_w) * 100), _f(wk_drift, 3)),
             _linked], seed=seed_map)

        # 下周：起点取前一交易日（本周预测期末）收盘，保证两周曲线连续
        _nw_prev = _prev_trade_day(nw_dates[0])
        nw_ref = (seed_map.get(_nw_prev) or {}).get("收盘") or actual_all.get(_nw_prev) or pc
        out["next_week"] = _wrap(
            "下周", nw_dates, nw_ref, nw_drift, "next_week",
            "起点为 %s 收盘（与前一交易日/本周卡末值连续），按「周线评分中枢 + 本周已实现动量」加权斜率逐日外推；"
            "预测段为确定性推演路径（非真实成交）；首个交易日与明日卡同源，两卡取值一致；未剔除法定节假日"
            % _nw_prev.strftime("%m-%d"),
            ["起点：%s 收盘 %s（前一周期末，避免两周曲线断开）" % (_nw_prev.strftime("%m-%d"), _px(nw_ref)),
             "周线评分 %s → 周中枢 %s%%；ATR14 %s%%；全周 %d 个交易日" %
             (wk_score, _f(wk_center), _f(atr), len(nw_dates)),
             "本周已实现动量 %s%%/日（权重 %d%%，上限 35%%）→ 外推斜率 %s%%/日" %
             (_pw(wk_mom), round(_ew(wk_mom, nw_w) * 100), _f(nw_drift, 3)),
             _linked], seed=seed_map)

        # 本月：实际已过部分 + 剩余交易日外推（近端链覆盖的交易日取链上值，其后按月中枢外推）
        rest_m = len([d0 for d0 in m_dates if d0 > base_d])
        out["month"] = _wrap(
            "本月", m_dates, prev_map.get(m_dates[0]) or pc, mo_drift, "month",
            "已过交易日取实际收盘（含今日盘中预收，即实线段）；下周五（含）之前的预测日取近端预测链（与明日/下周卡一致），"
            "其后 %d 个交易日按「月线评分中枢 + 月内已实现动量」加权斜率外推；未剔除法定节假日"
            % len([d0 for d0 in m_dates if d0 > base_d and d0 not in seed_map]),
            ["起点：本月首日前收盘 %s ｜ 月线评分 %s → 月中枢 %s%%（整月口径）" %
             (_px(prev_map.get(m_dates[0]) or pc), mo_score, _f(mo_center)),
             "月内已实现 %d 个交易日（与实际收盘逐日一致）｜最近动量 %s%%/日，权重 %d%%" %
             (mo_n, _pw(mo_mom), round(_ew(mo_mom, mo_w) * 100)),
             "合成后日均外推斜率 %s%%/日 → 剩余 %d 日累计中枢 %s%%" %
             (_f(mo_drift, 3), rest_m, _f(mo_drift * rest_m)),
             "ATR14 %s%%；动量项限幅 0.55×ATR/日，故实际走势明显偏离评分中枢时预测段会被拉向已实现方向，而非直接反转" % _f(atr),
             _linked], seed=seed_map)

        # 下月：两级外推（本月剩余 + 下月全月），中枢衰减 0.9
        nm_ref = out["month"].get("期末") or pc
        out["next_month"] = _wrap(
            "下月", nm_dates, nm_ref, nm_drift, "next_month",
            "以本月预测期末为锚（与本月卡末值连续）：月线评分中枢衰减 0.9 后叠加月内已实现动量，逐日外推全月；"
            "属「本月剩余 + 下月全月」两级外推，误差会累积，参考权重应低于近端周期；未剔除法定节假日",
            ["锚点：本月预测期末 %s（由本月剩余交易日外推而来，是预测值而非真实成交）" % _px(nm_ref),
             "月线评分 %s → 月中枢 %s%% × 衰减 0.9 = %s%%（整月口径）" %
             (mo_score, _f(mo_center), _f(mo_center * 0.9)),
             "继承本月动量 %s%%/日（权重 %d%%，低于本月的 %d%%）→ 外推斜率 %s%%/日" %
             (_pw(mo_mom), round(_ew(mo_mom, 0.30) * 100), round(_ew(mo_mom, mo_w) * 100), _f(nm_drift, 3))])

        # 本年度剩余月份（下月之后）：逐月链接、外推层级递增
        out["rest_months"] = []
        _lvl = 2
        _ref = out["next_month"].get("期末") or nm_ref
        _prev_label = "下月"
        _cur = nm_first
        while True:
            _cur = (_cur + timedelta(days=32)).replace(day=1)
            if _cur.year != base_d.year:
                break
            _lvl += 1
            _dst = _trade_dates(_cur, _month_end(_cur))
            if not _dst:
                break
            _w = max(0.15, 0.30 - 0.07 * (_lvl - 2))
            _dr = _blend_drift(mo_center * (0.9 ** (_lvl - 1)) / max(1, len(_dst)), mo_mom, _ew(mo_mom, _w))
            _blk = _wrap(
                "%d月" % _cur.month, _dst, _ref, _dr, "month",
                "以%s预测期末为锚（与上一张月度卡末值连续）：月线评分中枢按 0.9 逐级衰减后叠加月内已实现动量，"
                "逐日外推全月；属第 %d 级外推（本月剩余 → 下月 → 年内余月逐级传递），误差显著累积，"
                "仅供判断大方向，不可用于定价；未剔除法定节假日" % (_prev_label, _lvl),
                ["锚点：%s预测期末 %s（预测值而非真实成交）" % (_prev_label, _px(_ref)),
                 "月线评分 %s → 月中枢 %s%% × 衰减 %s = %s%%（整月口径）" %
                 (mo_score, _f(mo_center), _f(0.9 ** (_lvl - 1), 3), _f(mo_center * (0.9 ** (_lvl - 1)))),
                 "继承月内动量 %s%%/日（权重 %d%%，随外推层级递减）→ 外推斜率 %s%%/日" %
                 (_pw(mo_mom), round(_ew(mo_mom, _w) * 100), _f(_dr, 3)),
                 "外推层级 %d 级：越远离近端周期（明日/本周/下周），参考权重应越低" % _lvl])
            out["rest_months"].append({
                "名称": "%d年%d月" % (_cur.year, _cur.month),
                "区间": "%s ~ %s" % (_dst[0].strftime("%m-%d"), _dst[-1].strftime("%m-%d")),
                "到期日": _dst[-1].strftime("%Y-%m-%d"),
                "外推层级": _lvl,
                "区块": _blk,
            })
            _prev_label = "%d月" % _cur.month
            _ref = _blk.get("期末") or _ref

        # 本周剩余交易日（明日之后、本周之内；周五或明日已是本周最后交易日时无卡）
        nxt_d = _next_trade_day(base_d)
        rest_dates = [d0 for d0 in wk_dates if d0 > nxt_d]
        if rest_dates:
            r_ref = t_close
            out["week_rest"] = _wrap(
                "本周剩余", rest_dates, r_ref, wk_drift, "week", 
                "本周明日之后的剩余交易日：起点接明日卡预测收盘（与明日卡连续），沿用本周「评分中枢 + 已实现动量」"
                "加权斜率外推；日期与「本周 / 本月」卡重叠时取值一致；未剔除法定节假日",
                ["起点：明日（%s）预测收盘 %s（与明日卡连续）" % (nd.strftime("%m-%d"), _px(r_ref)),
                 "覆盖 %d 个交易日（%s ~ %s）" %
                 (len(rest_dates), rest_dates[0].strftime("%m-%d"), rest_dates[-1].strftime("%m-%d")),
                 "沿用本周斜率 %s%%/日（周中枢 %s%%、本周动量 %s%%/日、权重 %d%%）" %
                 (_f(wk_drift, 3), _f(wk_center), _pw(wk_mom), round(_ew(wk_mom, wk_w) * 100)),
                 _linked], seed=seed_map)

        # ---- 今日 / 明日 的坐标刻度与三维解读 ----
        t_ticks = [[j, _bar_clock(j)] for j in range(0, total - 1, 30)]
        if t_ticks and t_ticks[-1][0] != total - 1:
            t_ticks.append([total - 1, _bar_clock(total - 1)])
        today["X刻度"] = t_ticks
        today["维度"] = _dimensions("today", base_d, [base_d], ctx, q, ind, intl)
        today["依据"] = [
            "已走 %d/%d 点（%.1f%%），剩余时段按日线评分 %s → 中枢 %s%% 折算的剩余幅度线性外推" %
            (n_now, total, _f((n_now / float(total) * 100.0) if n_now else 0.0, 1), day_fc.get("评分"), _f(center)),
            "ATR14 %s%%；盘中最高/最低以现有高低点 ±0.25×ATR 包络；涨跌幅限制 ±%s%%" % (_f(atr), _f(limit, 1)),
            "预测段叠加 sin 包络修正（非随机），收盘价已按涨跌停约束截断",
        ]
        out["tomorrow"]["维度"] = _dimensions("tomorrow", nd, [nd], ctx, q, ind, intl)
        out["tomorrow"]["依据"] = [
            "以今日预测收盘 %s 为基准；日线评分 %s → 中枢 %s%%（按 0.85 折扣）" %
            (_px(pc), day_fc.get("评分"), _f(c1)),
            "跳空 %s%%（=中枢×0.3，限 ±0.35×ATR）；全天区间 = 中枢 ± 0.85×ATR14(%s%%)" % (_f(gap), _f(atr)),
            "全天 241 点按 开→冲高→探低→收 节奏生成，属确定性推演路径，非真实成交",
            "跨卡一致：本日开/高/低/收与 8 点采样路径写入近端预测链，本周剩余 / 下周 / 本月卡中的 %s 取同一组数值" % nd.strftime("%m-%d"),
        ]
        # ---- 预测风险提示（面板底部展示）----
        out["风险提示"] = [
            "本区走势为规则化统计推演：由「评分中枢 + ATR 波动 + 区间已实现动量」加权外推，不含基本面、消息面与突发事件，不构成投资建议。",
            "波动量级：ATR14 = %s%%，单日正常波动约 ±%s%%；预测段是与真实数据同量级的推演路径（非真实成交），实际走势可能大幅偏离。" % (_f(atr), _f(atr)),
            "涨跌幅限制 ±%s%%：触及涨/跌停、临停、停牌时段，预测路径与目标价位失效。" % _f(limit, 1),
            "事件风险：政策与监管、业绩预告/快报、并购重组、解禁减持、外围市场急跌等均未纳入模型，可能造成跳空式偏离。",
            "日历口径：交易日按「周一至周五」近似，未剔除法定节假日与调休，节前节后走势规律可能失真。",
            "数据口径：行情源自 thsdk 游客模式，若数据延迟、字段缺失或复权口径变化，预测基准（起点价 / ATR / 评分）会同步偏差。",
            "多级外推：下月 = 本月剩余交易日 + 下月全月两级外推，误差逐级累积，参考权重应低于近端周期。",
            "时效性：预测对应数据日期 %s 的快照，行情变化后需重新分析；历史预测不构成对未来的承诺。" % day_str,
        ]
        if out.get("rest_months"):
            out["风险提示"].append(
                "年内余月（%s）：属三/四级外推，锚点由本月与下月的「预测期末」逐级传递，误差叠加最重，"
                "仅可用于判断大方向，不可用于定价。" % "、".join(m["名称"] for m in out["rest_months"]))
        if win_dates:
            out["风险提示"].append(
                "跨卡一致：同一交易日在「明日 / 本周剩余 / 下周 / 本月」卡片中取同一「近端预测链」数值"
                "（同开盘、同最高、同最低、同收盘、同曲线形状），不会出现两条不同曲线；%s ~ %s 为链上统一定价。"
                % (win_dates[0].strftime("%m-%d"), win_dates[-1].strftime("%m-%d")))
        if ctx.get("is_fund"):
            out["风险提示"].append("基金/ETF 特有：预测基于二级市场价格，折溢价与场内流动性不足会导致成交价偏离净值。")
    except Exception:
        out["错误"] = traceback.format_exc(limit=2)
    return out


# --------------------------------------------------------------------------
# 持仓建议
# --------------------------------------------------------------------------
def build_position(shares, cost, q, ind, fc):
    close = q.get("收盘") or q.get("现价")
    res = {"持仓数量": shares, "成本价": cost, "现价": close}
    if not shares or not close:
        return res
    res["持仓市值"] = _f(shares * close, 0)
    if cost:
        res["浮动盈亏"] = _f((close - cost) * shares, 0)
        res["盈亏比例%"] = _f((close / cost - 1) * 100)
    day_s = fc.get("日", {}).get("评分", 0) or 0
    wk_s = fc.get("周", {}).get("评分", 0) or 0
    mo_s = fc.get("月", {}).get("评分", 0) or 0
    total = day_s * 0.3 + wk_s * 0.4 + mo_s * 0.3
    res["综合评分"] = _f(total, 0)

    ma20 = ind.get("MA20")
    lo20 = ind.get("近20日低")
    hi20 = ind.get("近20日高")
    atr_pct = (ind.get("ATR14%") or 2.0) / 100.0
    if close:
        vol_stop = close * (1 - 1.5 * atr_pct)
        if ma20 and close > ma20:
            sl = max(ma20 * 0.985, vol_stop)
        elif lo20:
            sl = max(lo20 * 0.99, vol_stop)
        else:
            sl = vol_stop
    else:
        sl = None
    tp = hi20 if hi20 else None
    res["止损参考位"] = _px(sl)
    res["止盈参考位"] = _px(tp)
    if sl and close:
        res["止损空间%"] = _f((sl / close - 1) * 100)

    if total >= 30:
        action = "偏多：可考虑持有/逢回调加仓"
        target = 0.8
    elif total >= 5:
        action = "中性偏多：持有为主，回踩不破支撑可小幅加仓"
        target = 0.6
    elif total > -5:
        action = "中性：持有观望，跌破止损位减仓"
        target = 0.5
    elif total > -30:
        action = "偏空：建议减仓，反弹到压力位降低仓位"
        target = 0.3
    else:
        action = "空头：建议以减仓/控制风险为主，严格止损"
        target = 0.2
    res["建议动作"] = action
    res["建议目标仓位占比"] = "%.0f%%" % (target * 100)

    if cost:
        pnl_pct = (close / cost - 1) * 100
        if pnl_pct <= -8 and total < 0:
            res["持仓提示"] = "浮亏已达 %.1f%% 且技术面偏弱，注意止损纪律" % pnl_pct
        elif pnl_pct >= 20 and total <= 30:
            res["持仓提示"] = "浮盈 %.1f%%，可考虑分批止盈锁定利润" % pnl_pct
        elif pnl_pct >= 50:
            res["持仓提示"] = "浮盈较大(%.1f%%)，建议设置移动止盈" % pnl_pct
        else:
            res["持仓提示"] = "浮盈亏 %.1f%%，按既定止损/止盈位持有" % pnl_pct
    return res


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _unavailable(ctx, val, cd):
    """汇总当前标的确实取不到的数据项及原因（按标的类型差异化）。"""
    na = {}
    if ctx.get("is_fund"):
        na["资金流（主力/超大单净额）"] = (
            "资金流接口不支持基金/ETF 标的，已用分价表 + 逐笔成交推导近似值（见资金块的「口径」字段）")
        na["跟踪误差"] = "问财无该字段元数据，游客模式未取到"
        if val.get("累计净值") is None:
            na["累计净值"] = "该标的未取到（部分基金不返回）"
        if val.get("场内份额") is None:
            na["场内份额"] = "该标的未取到（部分基金不返回）"
    else:
        na["折价率/折溢价率/跟踪指数/跟踪指数代码"] = "仅基金/ETF 标的适用"
        na["基金规模/份额/净值"] = "仅基金/ETF 标的适用"
    if cd[:3] in ("688", "689", "300", "301"):
        na["盘后固定价格(量/额)"] = "thsdk 游客模式无该接口权限，未取到"
    return na


def analyze(code, market=None, shares=0, cost=None, refresh_fields=False, quiet=True):
    warnings = []
    mkt, cd = (market, str(code)) if market else parse_code(code)
    _set_price_dec(None)      # 每次分析先复位，避免线程复用残留上一标的的精度设定
    ensure_auth()
    cache = resolve_fields(names=list(WENCAI_NAMES) + list(FUND_WENCAI_NAMES),
                           refresh=refresh_fields, quiet=quiet)
    ctx = collect(cd, mkt, warnings, cache)
    # 港股/港股通基金（含名称或跟踪指数命中港股关键词）：价格统一 3 位小数
    if ctx.get("is_fund") and _is_hk_fund(ctx.get("name"),
                                          (ctx.get("wencai") or {}).get("跟踪指数")):
        _set_price_dec(3)

    q = build_quote(ctx)
    ind = build_indicators(ctx)
    ob = build_order_book(ctx)
    fl = build_flows(ctx, q)
    tape = build_tape(ctx)
    pv = build_price_volume(ctx)
    per = build_periods(ctx)
    val = build_valuation(ctx)
    ctx["_ob"], ctx["_fl"], ctx["_tape"], ctx["_pv"] = ob, fl, tape, pv
    fc, tl = build_forecast(ctx, q, ind)
    outlook = build_outlook(ctx, q, ind, fc)

    intra = None
    idf = ctx.get("intraday")
    if idf is not None and len(idf):
        try:
            intra = {
                "点数": len(idf),
                "最新价": _px(idf["price"].iloc[-1]),
                "累计量": _f(idf["volume"].iloc[-1], 0),
                "累计额": _f(idf["turnover"].iloc[-1], 0),
                "均价(额/量)": _px(idf["turnover"].iloc[-1] / idf["volume"].iloc[-1])
                if idf["volume"].iloc[-1] else None,
            }
            seg = idf.tail(30)
            if len(seg) >= 2 and seg["volume"].iloc[0]:
                intra["近30分钟量能比(相对前段)"] = _f(
                    (seg["volume"].iloc[-1] - seg["volume"].iloc[0]) / max(seg["volume"].iloc[0], 1))
        except Exception:
            pass

    pos = build_position(shares, cost, q, ind, fc)

    if (q.get("收盘") is None and q.get("现价") is None) or (ctx.get("day") is None or len(ctx.get("day")) == 0):
        raise ValueError(
            "未取到 %s(%s) 的行情数据。该标的可能不在 thsdk 游客模式的可用范围内"
            "（当前覆盖：沪深A股、沪市基金/ETF（51/56/58 等）、深市基金/ETF（15/16/18）、"
            "部分北交所与 B 股）。" % (code, cd))

    fund_info = None
    if ctx.get("is_fund"):
        fund_info = {k: val.get(k) for k in
                     ("基金规模", "单位净值", "参考净值", "累计净值", "折价率",
                      "基金份额", "场内份额", "跟踪指数", "跟踪指数代码",
                      "管理费率", "基金类型", "基金成立日", "上市日期")}

    result = {
        "meta": {"输入": code, "market": mkt, "code": cd, "full_code": mkt + cd,
                 "名称": ctx.get("name"),
                 "标的类型": "基金/ETF" if ctx.get("is_fund") else "股票",
                 "价格小数位": _price_dec(),
                 "涨跌幅限制%": ctx.get("price_limit"),
                 "生成时间": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
                 "盘后固定价格适用": bool(cd[:3] in ("688", "689", "300", "301")),
                 "数据日期": _last_day(ctx)},
        "quote": q,
        "indicators": ind,
        "order_book": ob,
        "flows": fl,
        "tape": tape,
        "price_volume": pv,
        "periods": per,
        "min5": build_min5(ctx),
        "valuation": val,
        "fund": fund_info,
        "intraday": intra,
        "forecast": fc,
        "trendline": tl,
        "outlook": outlook,
        "series": _series(ctx),
        "position": pos,
        "unavailable": _unavailable(ctx, val, cd),
        "warnings": warnings,
    }
    _set_price_dec(None)      # 复位，避免线程复用把本次精度带到下一个标的
    return result


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _tbl(rows, headers=None):
    if not rows:
        return "_无数据_"
    headers = headers or list(rows[0].keys())
    out = ["| " + " | ".join(str(h) for h in headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join(
            "-" if r.get(h) is None else str(r.get(h)) for h in headers) + " |")
    return "\n".join(out)


def render_markdown(res):
    m = res["meta"]
    q = res["quote"]
    ind = res["indicators"]
    L = []
    A = L.append
    A("# MSA 股票分析报告 · %s(%s)" % (m.get("名称"), m.get("code")))
    A("")
    A("> 数据时间：%s（行情日期 %s） ｜ 标的类型：%s ｜ 涨跌幅限制：%s%% ｜ 数据源：同花顺 thsdk（游客模式）"
      % (m.get("生成时间"), m.get("数据日期") or "-", m.get("标的类型") or "股票",
         m.get("涨跌幅限制%") if m.get("涨跌幅限制%") is not None else 10.0))
    A("")

    A("## 一、行情快照")
    A("")
    A(_tbl([{
        "今开": q.get("今开"), "最高": q.get("最高"), "最低": q.get("最低"),
        "昨收": q.get("昨收"), "现价/收盘": q.get("收盘") or q.get("现价"),
        "涨跌幅%": q.get("涨跌幅"), "涨速%": q.get("涨速"), "均价": q.get("均价"),
        "总手": q.get("总手"), "金额": q.get("金额"),
        "量比": q.get("量比"), "换手率%": q.get("换手率"), "振幅%": q.get("振幅"),
    }]))
    A("")
    A("涨停价 %s ｜ 跌停价 %s ｜ 委比 %s ｜ 委差 %s ｜ 内盘 %s ｜ 外盘 %s"
      % (q.get("涨停价"), q.get("跌停价"), q.get("委比"), q.get("委差"),
         q.get("内盘"), q.get("外盘")))
    A("")

    A("## 二、技术指标")
    A("")
    A(_tbl([{
        "MA5": ind.get("MA5"), "MA10": ind.get("MA10"), "MA20": ind.get("MA20"),
        "MA60": ind.get("MA60"), "MA120": ind.get("MA120"),
        "DIF": ind.get("DIF"), "DEA": ind.get("DEA"), "MACD(M)": ind.get("MACD(M)"),
        "KDJ-K": ind.get("KDJ-K"), "KDJ-D": ind.get("KDJ-D"), "KDJ-J": ind.get("KDJ-J"),
        "RSI6": ind.get("RSI6"), "RSI12": ind.get("RSI12"), "RSI24": ind.get("RSI24"),
        "ATR14": ind.get("ATR14"), "ATR14%": ind.get("ATR14%"),
        "量比": q.get("量比"), "今昨成交比": ind.get("今昨成交比"),
    }]))
    A("")
    A("近20日高 %s ｜ 近20日低 %s ｜ 近60日高 %s ｜ 近60日低 %s ｜ 距60日高 %s%% ｜ 距60日低 %s%%"
      % (ind.get("近20日高"), ind.get("近20日低"), ind.get("近60日高"), ind.get("近60日低"),
         ind.get("近60日高距%"), ind.get("近60日低距%")))
    A("")

    A("## 三、五档盘口（买1-5 / 卖1-5）与大单")
    A("")
    ob = res["order_book"]
    rows = []
    for i in range(5):
        b = ob["买1-5"][i] if i < len(ob["买1-5"]) else {}
        a = ob["卖1-5"][i] if i < len(ob["卖1-5"]) else {}
        rows.append({"档位": i + 1, "卖价": a.get("价"), "卖量(手)": a.get("量(手)"),
                     "买价": b.get("价"), "买量(手)": b.get("量(手)")})
    A(_tbl(rows))
    A("")
    A("买五量 %s 手 ｜ 卖五量 %s 手 ｜ 买卖差 %s 手 ｜ 买卖力道 %s%%"
      % (ob.get("买五量(手)"), ob.get("卖五量(手)"), ob.get("买卖差"), ob.get("买卖力道")))
    A("")
    fl = res["flows"]
    if fl:
        if res["meta"].get("标的类型") == "基金/ETF":
            A("### 资金（基金标的：分价表 + 逐笔成交推导，近似口径）")
            A("")
            A(_tbl([{
                "主力净额(主买-主卖)": fl.get("主力净额"), "主力净占比%": fl.get("主力净占比%"),
                "主买额": fl.get("主买额"), "主卖额": fl.get("主卖额"),
                "大单净额(≥100万)": fl.get("大单净额"), "大单买入额": fl.get("大单买入额"),
                "大单卖出额": fl.get("大单卖出额"), "加权均价": fl.get("加权均价"),
                "成交额": fl.get("成交额"),
            }]))
            A("")
        else:
            A(_tbl([{
                "主力净额(主净买额)": fl.get("主力净额"), "主力净占比%": fl.get("主力净占比%"),
                "超大单净额": fl.get("超大单净额"), "大单净额": fl.get("大单净额"),
                "中单净额": fl.get("中单净额"), "大单净量%": fl.get("大单净量%"),
                "分时DDX": fl.get("分时DDX"), "成交额": fl.get("成交额"),
            }]))
            A("")

    pv = res["price_volume"]
    if pv.get("分价TOP"):
        A("### 分价成交（按成交量 TOP10）")
        A("")
        A(_tbl(pv["分价TOP"]))
        A("")
        A("主买占比 %s%% ｜ 主卖占比 %s%%" % (pv.get("主买占比%"), pv.get("主卖占比%")))
        A("")

    tape = res["tape"]
    gam = pv.get("分时博弈(全天)")
    if gam is not None or tape.get("分时博弈(逐笔抽样)") is not None:
        A("### 分时成交与博弈")
        A("")
        A("分时博弈(全天，主买-主卖占比差) %s%% ｜ 分时博弈(逐笔抽样) %s%% ｜ 逐笔抽样笔数 %s"
          % (gam, tape.get("分时博弈(逐笔抽样)"), tape.get("逐笔抽样笔数")))
        A("")
        A("主动买额(抽样) %s ｜ 主动卖额(抽样) %s ｜ 逐笔净额(抽样) %s"
          % (tape.get("主动买额(抽样)"), tape.get("主动卖额(抽样)"),
             tape.get("逐笔净额(抽样)")))
        A("")
    if tape.get("大单"):
        A("### 大单（单笔≥100万）")
        A("")
        A(_tbl(tape["大单"], ["时间", "价格", "量", "方向", "金额"]))
        A("")
    if tape.get("最近成交"):
        A("### 分时成交明细（最近20笔）")
        A("")
        A(_tbl(tape["最近成交"], ["时间", "价格", "量", "方向", "金额"]))
        A("")

    A("## 四、周期表现（7/14/28/57日）")
    A("")
    prows = []
    for k in ("7日", "14日", "28日", "57日"):
        if k in res["periods"]:
            d = res["periods"][k]
            prows.append({"周期": k, "区间涨跌幅%": d.get("区间涨跌幅%"),
                          "区间振幅%": d.get("区间振幅%"), "日均量": d.get("日均量"),
                          "日均额": d.get("日均额"), "区间最高": d.get("区间最高"),
                          "区间最低": d.get("区间最低")})
    A(_tbl(prows))
    A("")
    m5 = res.get("min5")
    if m5:
        A("### 近 8 根 5 分钟K线")
        A("")
        A(_tbl(m5))
        A("")
    if res.get("meta", {}).get("盘后固定价格适用"):
        A("> 盘后固定价格交易（15:05-15:30）：该标的适用，但 thsdk 游客模式无该字段权限，"
          "盘后固定价格成交量/成交额未取到。")
        A("")
    w = res["valuation"]
    if w:
        def _div(v, d):
            try:
                return _f(float(v) / d, 2)
            except Exception:
                return None

        if res["meta"].get("标的类型") == "基金/ETF":
            A("## 五、基金/ETF 专有数据")
            A("")
            scale_txt = _div(w.get("基金规模"), 1e8)
            if w.get("基金规模估算") and scale_txt is not None:
                scale_txt = "约%s" % scale_txt
            A(_tbl([{
                "基金规模(亿元)": scale_txt,
                "基金份额(亿份)": _div(w.get("基金份额"), 1e8),
                "场内份额(亿份)": _div(w.get("场内份额"), 1e8),
                "单位净值": w.get("单位净值"),
                "参考净值": w.get("参考净值"),
                "累计净值": w.get("累计净值"),
                "折溢价率%": w.get("折价率"),
                "跟踪指数": w.get("跟踪指数"),
                "跟踪指数代码": w.get("跟踪指数代码"),
                "管理费率%": w.get("管理费率"),
                "基金类型": w.get("基金类型"),
                "成立日": w.get("基金成立日"),
                "上市日": w.get("上市日期"),
            }]))
            A("")
            A("> 净值/规模/份额为最近披露口径；折溢价率 =（市价 / 参考净值 - 1）；"
              "标「约」的规模为「份额 × 净值」估算值。")
            A("")
        else:
            A("## 五、估值与股本")
            A("")
            A(_tbl([{"市盈率": w.get("市盈率"), "市净率": w.get("市净率"), "市销率": w.get("市销率"),
                     "总市值": w.get("总市值"), "流通市值": w.get("流通市值"),
                     "股息率%": w.get("股息率"), "总股本": w.get("总股本"),
                     "流通股本": w.get("流通股本"), "每股净资产": w.get("每股净资产"),
                     "实时估值": w.get("实时估值"), "最新份额": w.get("最新份额"),
                     "跟踪指数": w.get("跟踪指数"), "折价率": w.get("折价率")}]))
            A("")

    A("## 六、日/周/月涨幅预判与趋势线")
    A("")
    frows = []
    for k in ("日", "周", "月"):
        f = res["forecast"].get(k, {})
        rng = f.get("涨跌幅区间%") or [None, None]
        tgt = f.get("目标价区间") or [None, None]
        frows.append({"周期": k, "方向": f.get("方向"), "评分": f.get("评分"),
                      "预判涨跌幅区间%": "%s ~ %s" % (rng[0], rng[1]),
                      "中枢%": f.get("中枢%"), "预判目标价": "%s ~ %s" % (tgt[0], tgt[1])})
    A(_tbl(frows))
    A("")
    for k in ("日", "周", "月"):
        f = res["forecast"].get(k, {})
        A("**%s线预判依据**：%s" % (k, "；".join(f.get("依据") or ["—"])))
        A("")
    trows = []
    for k, t in (res.get("trendline") or {}).items():
        if not t:
            continue
        trows.append({"周期": k, "方向": t.get("direction"),
                      "斜率/期": _f(t.get("slope"), 3), "斜率%": _f(t.get("slope_pct"), 2),
                      "下一期拟合值": _px(t.get("next")), "上轨": _px(t.get("upper")),
                      "下轨": _px(t.get("lower")), "样本数": t.get("sample")})
    A("**趋势线（最小二乘，±1.5σ）**")
    A("")
    A(_tbl(trows))
    A("")
    A("> 预判基于量价、资金、指标与波动率的规则化打分，属于统计参考，**不构成投资建议**。")
    A("")
    risk = (res.get("outlook") or {}).get("风险提示") or []
    if risk:
        A("**预测风险提示**")
        A("")
        for r in risk:
            A("- %s" % r)
        A("")

    pos = res["position"]
    A("## 七、持仓情况与建议")
    A("")
    if not pos.get("持仓数量"):
        A("未提供持仓数量，暂不生成持仓建议。可带参数 `--shares 持仓股数 --cost 成本价` 重新运行。")
    else:
        A(_tbl([{
            "持仓数量": pos.get("持仓数量"), "成本价": pos.get("成本价"), "现价": pos.get("现价"),
            "持仓市值": pos.get("持仓市值"), "浮动盈亏": pos.get("浮动盈亏"),
            "盈亏比例%": pos.get("盈亏比例%"), "综合评分": pos.get("综合评分"),
            "止损参考位": pos.get("止损参考位"), "止盈参考位": pos.get("止盈参考位"),
            "止损空间%": pos.get("止损空间%"),
        }]))
        A("")
        A("**建议动作**：%s" % pos.get("建议动作"))
        A("")
        A("建议目标仓位占比：%s" % pos.get("建议目标仓位占比"))
        if pos.get("持仓提示"):
            A("")
            A("提示：%s" % pos.get("持仓提示"))
    A("")

    if res.get("unavailable"):
        A("## 附：未取到的数据项")
        A("")
        for k_na, v_na in res["unavailable"].items():
            A("- **%s**：%s" % (k_na, v_na))
        A("")

    if res.get("warnings"):
        A("## 附：数据获取提示")
        A("")
        for wtxt in dict.fromkeys(res["warnings"]):
            A("- %s" % wtxt)
        A("")
    A("---")
    A("本报告由 MSA 技能自动生成，仅供研究参考，不构成任何投资建议。")
    return "\n".join(L)


def compact_summary(res):
    """终端一屏摘要（用于每分钟轮询）。"""
    q, ind, fc = res["quote"], res["indicators"], res["forecast"]
    pos = res["position"]
    lines = []
    lines.append("[%s] %s(%s) 现价 %s 涨跌 %s%% 量比 %s 换手 %s%%"
                 % (res["meta"]["生成时间"], res["meta"].get("名称"), res["meta"]["code"],
                    q.get("收盘") or q.get("现价"), q.get("涨跌幅"),
                    q.get("量比"), q.get("换手率")))
    lines.append("  主力净额 %s ｜ 买卖力道 %s%% ｜ 分时博弈 %s%%"
                 % (res["flows"].get("主力净额"), res["order_book"].get("买卖力道"),
                    res["price_volume"].get("分时博弈(全天)")
                    if res["price_volume"].get("分时博弈(全天)") is not None
                    else res["tape"].get("分时博弈(逐笔抽样)")))
    for k in ("日", "周", "月"):
        f = fc.get(k, {})
        rng = f.get("涨跌幅区间%") or [None, None]
        lines.append("  %s线预判: %s %s%%~%s%% (评分 %s)"
                     % (k, f.get("方向"), rng[0], rng[1], f.get("评分")))
    if pos.get("持仓数量"):
        lines.append("  持仓 %s 股 ｜ 市值 %s ｜ 浮盈亏 %s (%s%%) ｜ %s"
                     % (pos.get("持仓数量"), pos.get("持仓市值"), pos.get("浮动盈亏"),
                        pos.get("盈亏比例%"), pos.get("建议动作")))
    fnd = res.get("fund")
    if fnd:
        try:
            scale = "%.2f" % (float(fnd["基金规模"]) / 1e8)
        except Exception:
            scale = "-"
        lines.append("  基金: 单位净值 %s ｜ 参考净值 %s ｜ 折溢价率 %s%% ｜ 规模 %s 亿元 ｜ 跟踪 %s"
                     % (fnd.get("单位净值"), fnd.get("参考净值"), fnd.get("折价率"),
                        scale, fnd.get("跟踪指数") or "-"))
    return "\n".join(lines)
