# -*- coding: utf-8 -*-
"""msa 多分析模型引擎：把免费股票分析接口统一为面板可多选的「分析模型」。

设计
----
1. 注册表 MODELS：id / 名称 / 分组 / 颜色 / 对应库 / 说明 / 官网，前后端共用（/api/models）。
2. 每个模型 = 一种经典分析方法论，用面板统一的本地行情（thsdk / 问财 / 腾讯 / 新浪）纯
   numpy/pandas 实现，输出统一结构：评分(-100~100)、立场、20 日目标涨跌幅、区间、
   未来 20 个交易日预测路径、比例尺备注、依据。
3. 多模型曲线共用同一价格轴，颜色区分，前端图例标注各模型比例尺。
4. 若对应第三方库已安装（akshare / baostock / tushare / efinance / yfinance / openbb /
   quandl），标注 installed=True 仅作口径说明；行情仍取面板统一实时源，保证曲线可比。
"""
from __future__ import annotations

import datetime as _dt
import math

MODELS = [
    {"id": "msa", "name": "MSA 原生预判", "group": "面板内置", "color": "#f5a623",
     "lib": None, "desc": "面板原生日/周/月评分中枢 + ATR 外推", "url": ""},
    {"id": "akshare", "name": "AkShare 多因子", "group": "国内免费接口", "color": "#3d8bff",
     "lib": "akshare", "desc": "动量 + 量价 + 资金流多因子合成（全品类公开数据口径）",
     "url": "https://akshare.akfamily.xyz"},
    {"id": "baostock", "name": "Baostock 均线回测", "group": "国内免费接口", "color": "#2fbf71",
     "lib": "baostock", "desc": "MA5/MA10/MA20 排列与金叉回测（A 股历史行情口径）",
     "url": "http://baostock.com"},
    {"id": "tushare", "name": "Tushare 估值因子", "group": "国内免费接口", "color": "#e05252",
     "lib": "tushare", "desc": "估值分位 + 均值回归（财务与因子数据口径）",
     "url": "https://tushare.pro"},
    {"id": "efinance", "name": "efinance 资金流", "group": "国内免费接口", "color": "#a06bff",
     "lib": "efinance", "desc": "主力 / 大单净流入驱动的短期方向（东方财富口径）",
     "url": "https://github.com/Micro-sheep/efinance"},
    {"id": "alphavantage", "name": "Alpha Vantage 技术指标", "group": "海外免费接口",
     "color": "#f2c14e", "lib": "alpha_vantage",
     "desc": "MACD + RSI + KDJ 组合信号（50+ 技术指标口径）",
     "url": "https://www.alphavantage.co"},
    {"id": "yfinance", "name": "yfinance 趋势跟踪", "group": "海外免费接口", "color": "#00b3a4",
     "lib": "yfinance", "desc": "EMA12/EMA26 双均线交叉 + 斜率趋势跟踪",
     "url": "https://github.com/ranaroussi/yfinance"},
    {"id": "openbb", "name": "OpenBB 综合评分", "group": "海外免费接口", "color": "#ff7ab6",
     "lib": "openbb", "desc": "多源综合加权评分（开源版彭博终端口径）",
     "url": "https://openbb.co"},
    {"id": "quandl", "name": "Quandl 长周期因子", "group": "海外免费接口", "color": "#8b98a9",
     "lib": "quandl", "desc": "长周期趋势 + 波动率通道（宏观 / 商品数据集口径）",
     "url": "https://data.nasdaq.com"},
]

MODEL_MAP = {m["id"]: m for m in MODELS}
DEFAULT_MODELS = [m["id"] for m in MODELS]   # 默认全选：面板首屏即呈现全部模型曲线
HORIZON = 20          # 预测交易日数


# ---------------------------------------------------------------- 基础工具
def _num(v, d=None):
    try:
        f = float(v)
        return d if (f != f or f in (float("inf"), float("-inf"))) else f
    except Exception:
        return d


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def _closes(ctx, key="day", n=140):
    df = ctx.get(key)
    if df is None:
        return []
    try:
        vals = [float(x) for x in df["close"].tolist()]
        return [v for v in vals if v == v][-n:]
    except Exception:
        return []


def _ma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def _ema(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    out = [float(vals[0])]
    for v in vals[1:]:
        out.append(out[-1] + k * (float(v) - out[-1]))
    return out


def _rsi(vals, n=14):
    if len(vals) < n + 1:
        return 50.0
    gain = loss = 0.0
    for i in range(len(vals) - n, len(vals)):
        d = vals[i] - vals[i - 1]
        if d > 0:
            gain += d
        else:
            loss -= d
    if loss <= 0:
        return 100.0
    rs = (gain / n) / (loss / n)
    return 100.0 - 100.0 / (1.0 + rs)


def _slope_pct(vals, n):
    """最近 n 段线性斜率，换算成 n 日涨跌幅%。"""
    if len(vals) < n + 1 or not vals[-n - 1]:
        return 0.0
    return (vals[-1] / vals[-n - 1] - 1.0) * 100.0


def _ret(vals, n):
    if len(vals) < n + 1 or not vals[-n - 1]:
        return 0.0
    return (vals[-1] / vals[-n - 1] - 1.0) * 100.0


def _live(q):
    for k in ("现价", "收盘"):
        v = _num((q or {}).get(k))
        if v:
            return v
    return None


def _stance(score):
    if score >= 45:
        return "看多"
    if score >= 15:
        return "偏多"
    if score > -15:
        return "震荡"
    if score > -45:
        return "偏空"
    return "看空"


def _future_dates(n, start=None):
    d = start or _dt.date.today()
    out = []
    while len(out) < n:
        d = d + _dt.timedelta(days=1)
        if d.weekday() < 5:
            out.append(d.isoformat())
    return out


def _path(p0, target_pct, atr_pct, n=HORIZON, curve=1.0, phase=0.0):
    """生成 n 段预测路径：起点 p0，终点 p0*(1+target%)，途中叠加受控波动（确定性）。"""
    if not p0:
        return []
    tgt = p0 * (1.0 + target_pct / 100.0)
    amp = p0 * max(atr_pct, 0.4) / 100.0 * 0.30
    out = [round(p0, 4)]
    for i in range(1, n + 1):
        r = i / float(n)
        base = p0 + (tgt - p0) * (r ** curve)
        wave = amp * math.sin(r * math.pi * 2.2 * (1.0 + phase)) * ((1.0 - r) ** 0.5)
        out.append(round(base + wave, 4))
    return out


def _target_from_score(score, atr_pct, limit=10.0):
    """评分 → 20 日目标涨跌幅%（以 ATR 为尺度，受涨跌幅限制约束）。"""
    amp = max(_num(atr_pct, 2.0) or 2.0, 0.4)
    t = score / 100.0 * amp * math.sqrt(HORIZON) * 0.62
    cap = max(limit, 5.0) * 2.2
    return _clamp(t, -cap, cap)


def _val_of(val, *keys):
    """估值字典模糊取键：PE / PB 等键名可能随口径变化。"""
    if not isinstance(val, dict):
        return None
    for k in keys:
        for kk, vv in val.items():
            if k in str(kk):
                n = _num(vv)
                if n is not None:
                    return n
    return None


def _score_norm(x, scale):
    """把任意量纲指标压缩到 -100~100（tanh 软饱和）。"""
    try:
        return _clamp(math.tanh(float(x) / float(scale)) * 100.0, -100.0, 100.0)
    except Exception:
        return 0.0


# ---------------------------------------------------------------- 各模型
def _m_msa(ctx, q, ind, fl, val, fc):
    day_fc = (fc or {}).get("日") or {}
    wk_fc = (fc or {}).get("周") or {}
    d = _num(day_fc.get("评分"), 0.0) or 0.0
    w = _num(wk_fc.get("评分"), 0.0) or 0.0
    score = _clamp(d * 0.65 + w * 0.35, -100, 100)
    return score, ["面板原生评分：日线 %.0f / 周线 %.0f（按 0.65/0.35 加权）" % (d, w)]


def _m_akshare(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    mom = _ret(c, 20)
    vr = _num(ind.get("今昨成交比"), 1.0) or 1.0
    flow = _num((fl or {}).get("主力净占比%"), 0.0) or 0.0
    pos = _num(ind.get("近60日高距%"), 0.0) or 0.0
    s = (_score_norm(mom, 12.0) * 0.40
         + _score_norm((vr - 1.0) * 100.0, 60.0) * 0.20
         + _score_norm(flow, 6.0) * 0.25
         + _score_norm(pos, 12.0) * 0.15)
    return _clamp(s, -100, 100), [
        "20 日动量 %+.2f%%" % mom,
        "量能比 %.2f" % vr,
        "主力净占比 %+.2f%%" % flow,
        "距 60 日高 %.2f%%" % (-pos if pos else 0.0),
    ]


def _m_baostock(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    ma5, ma10, ma20 = _ma(c, 5), _ma(c, 10), _ma(c, 20)
    if not (ma5 and ma10 and ma20):
        return 0.0, ["日线样本不足 20 根，均线系统暂不可用"]
    px = c[-1]
    s = (_score_norm((px / ma5 - 1) * 100, 3.0) * 0.25
         + _score_norm((ma5 / ma10 - 1) * 100, 2.0) * 0.25
         + _score_norm((ma10 / ma20 - 1) * 100, 3.0) * 0.30
         + _score_norm((px / ma20 - 1) * 100, 6.0) * 0.20)
    cross = "多头排列" if ma5 > ma10 > ma20 else ("空头排列" if ma5 < ma10 < ma20 else "均线纠缠")
    return _clamp(s, -100, 100), [
        "MA5 %.3f / MA10 %.3f / MA20 %.3f" % (ma5, ma10, ma20),
        "现价对 MA20 %+.2f%%" % ((px / ma20 - 1) * 100),
        cross,
    ]


def _m_tushare(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    pe = _val_of(val, "市盈率", "PE")
    pb = _val_of(val, "市净率", "PB")
    px = c[-1] if c else _live(q)
    ma60 = _ma(c, 60)
    s = 0.0
    notes = []
    if ma60 and px:
        dev = (px / ma60 - 1) * 100
        s += _score_norm(-dev, 14.0) * 0.55          # 偏离均值越多 → 回归压力越大
        notes.append("现价对 MA60 %+.2f%%（均值回归口径）" % dev)
    else:
        notes.append("MA60 样本不足，改用 20 日区间定位")
        s += _score_norm(_num(ind.get("近60日低距%"), 0.0) or 0.0, 15.0) * 0.30
    if pe is not None:
        s += _score_norm(20.0 - pe, 40.0) * 0.28
        notes.append("PE %.2f" % pe)
    if pb is not None:
        s += _score_norm(2.0 - pb, 4.0) * 0.17
        notes.append("PB %.2f" % pb)
    if pe is None and pb is None:
        notes.append("未取到 PE/PB，按趋势因子折算")
    return _clamp(s, -100, 100), notes


def _m_efinance(ctx, q, ind, fl, val, fc):
    fl = fl or {}
    main = _num(fl.get("主力净额"), 0.0) or 0.0
    big = _num(fl.get("大单净额"), 0.0) or 0.0
    amt = _num(fl.get("成交额"), 0.0) or 0.0
    ddx = _num(fl.get("分时DDX"), 0.0) or 0.0
    ratio = (main / amt * 100.0) if amt else 0.0
    s = (_score_norm(ratio, 6.0) * 0.55
         + _score_norm(ddx, 2.5) * 0.25
         + _score_norm((big - main) / amt * 100.0 if amt else 0.0, 4.0) * 0.20)
    return _clamp(s, -100, 100), [
        "主力净额 %.0f（占成交 %+.2f%%）" % (main, ratio),
        "大单净额 %.0f" % big,
        "分时 DDX %+.3f" % ddx,
    ]


def _m_alphavantage(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    dif = _num(ind.get("DIF"), 0.0) or 0.0
    dea = _num(ind.get("DEA"), 0.0) or 0.0
    macd = _num(ind.get("MACD(M)"), (dif - dea) * 2) or 0.0
    j = _num(ind.get("KDJ-J"), 50.0) or 50.0
    rsi = _rsi(c) if len(c) > 20 else 50.0
    s = (_score_norm(macd, max(abs(_num(ind.get("ATR14"), 1.0) or 1.0), 0.05)) * 0.36
         + _score_norm(50.0 - j, 45.0) * 0.24         # 超买回落 / 超卖反弹
         + _score_norm(50.0 - rsi, 35.0) * 0.24
         + (25.0 if ind.get("MACD金叉") else (-25.0 if ind.get("MACD多头") is False else 0.0)) * 0.16)
    return _clamp(s, -100, 100), [
        "MACD 柱 %.3f（DIF %.3f / DEA %.3f）" % (macd, dif, dea),
        "KDJ-J %.1f · RSI14 %.1f" % (j, rsi),
        ("MACD 金叉" if ind.get("MACD金叉") else "MACD 无金叉信号"),
    ]


def _m_yfinance(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    if len(c) < 30:
        return 0.0, ["日线样本不足 30 根，EMA 趋势暂不可用"]
    e12, e26 = _ema(c, 12)[-1], _ema(c, 26)[-1]
    sl = _slope_pct(c, 10)
    s = (_score_norm((e12 / e26 - 1) * 100, 3.0) * 0.60
         + _score_norm(sl, 8.0) * 0.40)
    return _clamp(s, -100, 100), [
        "EMA12 %.3f / EMA26 %.3f（差 %+.2f%%）" % (e12, e26, (e12 / e26 - 1) * 100),
        "近 10 日斜率 %+.2f%%" % sl,
        "双均线" + ("金叉向上" if e12 > e26 else "死叉向下"),
    ]


def _m_quandl(ctx, q, ind, fl, val, fc):
    c = _closes(ctx)
    if len(c) < 70:
        return 0.0, ["日线样本不足 70 根，长周期因子暂不可用"]
    ma20, ma60 = _ma(c, 20), _ma(c, 60)
    long_slope = _slope_pct(c, 60)
    vol = _num(ind.get("ATR14%"), 2.0) or 2.0
    rets = [(c[i] / c[i - 1] - 1.0) * 100 for i in range(1, len(c))]
    recent = rets[-60:]
    mu = sum(recent) / len(recent) if recent else 0.0
    var = sum((x - mu) ** 2 for x in recent) / len(recent) if recent else 0.0
    vol60 = math.sqrt(var)
    s = (_score_norm(long_slope, 18.0) * 0.55
         + _score_norm((ma20 / ma60 - 1) * 100 if (ma20 and ma60) else 0.0, 6.0) * 0.25
         + _score_norm((vol60 - vol) * 10.0, 30.0) * 0.20)
    return _clamp(s, -100, 100), [
        "60 日长周期斜率 %+.2f%%" % long_slope,
        "MA20/MA60 %s" % ("上穿" if (ma20 and ma60 and ma20 > ma60) else "下穿"),
        "60 日日均波动 %.2f%%（当日 ATR %.2f%%）" % (vol60, vol),
    ]


_BASE = {
    "akshare": _m_akshare,
    "baostock": _m_baostock,
    "tushare": _m_tushare,
    "efinance": _m_efinance,
    "alphavantage": _m_alphavantage,
    "yfinance": _m_yfinance,
    "quandl": _m_quandl,
}


def _m_openbb(ctx, q, ind, fl, val, fc, cache):
    """OpenBB 口径：对其它模型做多源加权综合。"""
    parts, notes = [], []
    for mid in ("akshare", "baostock", "efinance", "alphavantage", "yfinance"):
        item = cache.get(mid)
        if item:
            parts.append(item["score"])
            notes.append("%s %+.0f" % (item["short"], item["score"]))
    if not parts:
        return 0.0, ["缺少可综合的子模型结果"]
    return _clamp(sum(parts) / len(parts), -100, 100), [
        "多源加权综合：" + " / ".join(notes),
    ]


def _installed(lib):
    if not lib:
        return None
    try:
        __import__(lib)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- 入口
def model_list():
    """返回注册表（附各模型对应库的本地安装状态）。"""
    out = []
    for m in MODELS:
        d = dict(m)
        d["installed"] = _installed(m.get("lib"))
        out.append(d)
    return out


def run(ctx, q, ind, fl, val, fc, model_ids=None):
    """计算所选模型的评分、目标涨跌幅与未来路径曲线。

    model_ids: list[str] | str(逗号分隔) | None（None → 默认 MSA 原生）
    """
    if isinstance(model_ids, str):
        raw = model_ids.strip()
        if raw in ("all", "*"):
            model_ids = [m["id"] for m in MODELS]
        elif raw in ("none", "-", "0", ""):
            model_ids = []
        else:
            model_ids = [x.strip() for x in raw.split(",") if x.strip()]
    if model_ids is None:
        model_ids = list(DEFAULT_MODELS)
    ids = [i for i in model_ids if i in MODEL_MAP]
    if not ids and model_ids:              # 传了但全非法 → 回退默认，避免静默空跑
        ids = list(DEFAULT_MODELS)
    seen, ordered = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)

    if not ordered:
        return {"selected": [], "count": 0, "models": [], "dates": [],
                "note": "未选择任何分析模型"}   # 用户取消全部模型：不出多模型分析

    p0 = _live(q)
    if not p0:
        return {"selected": ordered, "models": [], "dates": [], "error": "未取到实时价，无法生成模型曲线"}
    atr_pct = _num(ind.get("ATR14%"), 2.0) or 2.0
    limit = _num(ctx.get("price_limit"), 10.0) or 10.0
    dates = _future_dates(HORIZON)

    cache = {}
    for mid in ordered:
        if mid == "openbb":
            continue
        try:
            if mid == "msa":
                score, basis = _m_msa(ctx, q, ind, fl, val, fc)
            else:
                fn = _BASE.get(mid)
                if fn is None:
                    continue
                score, basis = fn(ctx, q, ind, fl, val, fc)
            sl = score / 100.0 * max(atr_pct, 0.4) * 1.6
            cache[mid] = {"score": round(score, 2),
                          "short": MODEL_MAP[mid]["name"].split()[0],
                          "drift": round(sl, 3),
                          "basis": list(basis or [])}
        except Exception as exc:
            cache[mid] = {"score": 0.0, "short": MODEL_MAP[mid]["name"].split()[0],
                          "drift": 0.0, "basis": ["计算异常：%s" % str(exc)[:80]]}

    if "openbb" in ordered:
        try:
            s_ob, b_ob = _m_openbb(ctx, q, ind, fl, val, fc, cache)
            cache["openbb"] = {"score": round(s_ob, 2), "short": "OpenBB",
                               "basis": list(b_ob or []),
                               "drift": round(s_ob / 100.0 * max(atr_pct, 0.4) * 1.6, 3)}
        except Exception as exc:
            cache["openbb"] = {"score": 0.0, "short": "OpenBB", "drift": 0.0,
                               "basis": ["计算异常：%s" % str(exc)[:80]]}

    out = []
    for idx, mid in enumerate(ordered):
        meta = MODEL_MAP[mid]
        item = cache.get(mid)
        if item is None:
            continue
        score = item["score"]
        target = _target_from_score(score, atr_pct, limit)
        band = max(atr_pct * math.sqrt(HORIZON) * 0.55, 0.5)
        curve = _path(p0, target, atr_pct, HORIZON,
                      curve=1.0 + 0.22 * (idx % 3), phase=idx * 0.35)
        vol = max(abs(target) * 0.45 + atr_pct * 0.8, 0.8)
        out.append({
            "id": mid, "name": meta["name"], "group": meta["group"], "color": meta["color"],
            "lib": meta.get("lib"), "installed": _installed(meta.get("lib")),
            "score": round(score, 2),
            "stance": _stance(score),
            "drift_per_day": round(score / 100.0 * max(atr_pct, 0.4) * 1.6, 3),
            "target_pct": round(target, 2),
            "target_price": round(p0 * (1 + target / 100.0), 3),
            "band_pct": round(band, 2),
            "band_range": [round(p0 * (1 + (target - band) / 100.0), 3),
                           round(p0 * (1 + (target + band) / 100.0), 3)],
            "low_px": round(p0 * (1 + (target - band) / 100.0), 3),
            "high_px": round(p0 * (1 + (target + band) / 100.0), 3),
            "scale": "%+.2f%%" % target,
            "vol_pct": round(vol, 2),
            "curve": curve,
            "basis": list(item.get("basis") or []),
        })

    return {
        "selected": ordered,
        "count": len(out),
        "base_price": round(p0, 4),
        "atr_pct": round(atr_pct, 2),
        "horizon": HORIZON,
        "dates": dates,
        "models": out,
        "note": "各模型为对应接口经典方法论的本地实现，行情取面板统一实时源，"
                "统计口径、非投资建议；第三方库未安装时不影响本地算法运行。",
    }


def compact(res_models):
    """给 markdown 报告用的一行摘要。"""
    if not res_models or not res_models.get("models"):
        return ""
    parts = ["%s %s(%+.2f%%)" % (m["name"], m["stance"], m["target_pct"])
             for m in res_models["models"]]
    return "多模型结论：" + "；".join(parts)
