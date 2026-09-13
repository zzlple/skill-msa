# -*- coding: utf-8 -*-
"""MSA 基本面 / 突发事件评估 —— 为纪律线提供外部参数。

数据源（公开、免鉴权，仅标准库）：
  1) 东方财富全文搜索接口 search-api-web.eastmoney.com：抓取标的相关的新闻 / 公告标题与摘要
  2) 东方财富基金页 pingzhongdata：ETF 净值 / 规模（作为基本面兜底）
  3) 复用 msa_core 已给出的 fund / valuation 字段（调用方传入）

输出（供前端纪律线叠加）：
  sentiment   事件情绪分 -1 ~ +1（负=偏空）
  impact      冲击等级 low / medium / high
  adjust      修正系数 {stop_shift_atr, target_shift_atr, buy_gate_mult, sell_first}
  events      事件列表（标题 / 时间 / 来源 / 单条情绪 / 链接）
  basis       基本面摘要（折溢价 / 规模 / 净值等）

缓存：<skill>/.cache/events_<code>.json（TTL 默认 30 分钟），抓取失败时回退旧缓存并标注 stale。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
import urllib.request

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
CACHE_TTL = 1800                     # 30 分钟
_HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.path.dirname(_HERE), ".cache")

# ---------------------------------------------------------------- 词表
# 关键词 -> 权重（正=利好 / 负=利空）；同时决定事件类别
_WORDS = {
    # 政策 / 监管（对 ETF、创新药权重高）
    "医保": 1, "集采": -1, "降价": -1, "控费": -1, "审批": 0.4, "获批": 1, "药监局": 0.5,
    "政策支持": 1, "补贴": 1, "减税": 1, "纳入": 1, "扩容": 1, "试点": 0.6, "指导意见": 0.4,
    "监管": -0.6, "立案": -1, "调查": -0.8, "处罚": -1, "违规": -1, "退市": -1.2,
    "关税": -0.6, "出口管制": -1, "制裁": -1.2, "反垄断": -0.6,
    # 业绩 / 公司
    "增长": 0.8, "超预期": 1.2, "扭亏": 1, "预增": 1.1, "新高": 0.8, "扭亏为盈": 1.1,
    "下滑": -0.9, "亏损": -1, "不及预期": -1, "预减": -1, "商誉减值": -1, "计提": -0.7,
    "订单": 0.8, "中标": 0.9, "签约": 0.7, "合作": 0.5, "授权": 0.8, "出海": 0.7,
    "回购": 1, "增持": 1, "减持": -1, "质押": -0.6, "解禁": -0.7, "分红": 0.6,
    "终止": -0.8, "停产": -1, "召回": -1, "裁员": -0.7, "破产": -1.5, "违约": -1.4,
    "诉讼": -0.8, "下调": -0.8, "上调": 0.8, "目标价": 0.5, "评级": 0.6,
    # 市场 / 资金
    "涨停": 1, "上涨": 0.5, "大涨": 0.9, "反弹": 0.4, "突破": 0.6, "放量": 0.3,
    "跌停": -1.2, "下跌": -0.6, "大跌": -1, "回调": -0.3, "破位": -0.8,
    "净流入": 0.7, "流入": 0.4, "净流出": -0.7, "流出": -0.4, "资金": 0.1,
    "风险提示": -0.8, "退市风险": -1.2, "业绩变脸": -1,
}

# 类别权重：政策 > 公司 > 市场
_CAT_RULES = (
    ("政策", 1.25, ("医保", "集采", "药监局", "监管", "政策", "审批", "获批", "纳入",
                    "补贴", "关税", "出口管制", "制裁", "立案", "处罚", "试点")),
    ("公司", 1.0, ("业绩", "订单", "中标", "签约", "回购", "增持", "减持", "亏损", "增长",
                   "减值", "终止", "诉讼", "合作", "授权", "出海", "评级", "目标价", "分红")),
    ("市场", 0.6, ("涨停", "跌停", "上涨", "下跌", "资金", "流入", "流出", "回调", "反弹",
                   "成交", "换手", "估值", "溢价", "折价")),
)


# ---------------------------------------------------------------- 工具
def _http_get(url, timeout=12, referer="https://so.eastmoney.com/", retry=2):
    last = None
    for i in range(max(1, retry)):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Referer": referer,
            })
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except Exception:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as e:          # 含反爬断连，短暂退避后重试
            last = e
            time.sleep(0.6 + 0.6 * i)
    raise last


def _strip_html(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def _age_weight(date_str):
    """按新闻时间做衰减：6h=1.0 / 24h=0.85 / 72h=0.65 / 更早=0.45"""
    try:
        t = time.mktime(time.strptime(date_str.strip(), "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return 0.6
    hours = max(0.0, (time.time() - t) / 3600.0)
    if hours <= 6:
        return 1.0
    if hours <= 24:
        return 0.85
    if hours <= 72:
        return 0.65
    return 0.45


def _score_text(text):
    """返回 (分值, 命中明细)；分值 = Σ 权重，明细为 [(词, 权重)]"""
    hits = []
    total = 0.0
    for w, wt in _WORDS.items():
        if w in text:
            total += wt
            hits.append((w, wt))
    return total, hits


def _category(text):
    for name, wt, kws in _CAT_RULES:
        if any(k in text for k in kws):
            return name, wt
    return "其它", 0.8


# ---------------------------------------------------------------- 抓取
def fetch_news(keyword, size=14, timeout=12):
    """东方财富全文搜索：返回 [{date,title,content,source,url}]"""
    param = {
        "uid": "",
        "keyword": keyword,
        "type": ["cmsArticleWebOld"],
        "client": "web", "clientType": "web", "clientVersion": "curr",
        "param": {"cmsArticleWebOld": {
            "searchScope": "default", "sort": "time",
            "pageIndex": 1, "pageSize": size, "preTag": "", "postTag": "",
        }},
    }
    url = ("https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param="
           + urllib.parse.quote(json.dumps(param, ensure_ascii=False)))
    txt = _http_get(url, timeout=timeout)
    m = re.search(r"^\s*\w+\((.*)\)\s*;?\s*$", txt, re.S)
    if not m:
        return []
    try:
        obj = json.loads(m.group(1))
    except Exception:
        return []
    rows = ((obj.get("result") or {}).get("cmsArticleWebOld")) or []
    out = []
    for r in rows:
        out.append({
            "date": (r.get("date") or "").strip(),
            "title": _strip_html(r.get("title")),
            "content": _strip_html(r.get("content"))[:180],
            "source": (r.get("mediaName") or "").strip(),
            "url": (r.get("url") or "").strip(),
        })
    return out


def fetch_fund_basis(code, timeout=10):
    """东方财富基金页 pingzhongdata：取 ETF 净值 / 规模，作为基本面兜底。失败返回 {}"""
    try:
        js = _http_get("https://fund.eastmoney.com/pingzhongdata/%s.js" % code, timeout=timeout)
    except Exception:
        return {}
    out = {}
    try:
        m = re.search(r"fS_name\s*=\s*\"([^\"]*)\"", js)
        if m:
            out["名称"] = m.group(1)
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", js, re.S)
        if m:
            arr = json.loads(m.group(1))
            if arr:
                out["单位净值"] = round(float(arr[-1].get("y", 0)), 4)
                out["净值日期"] = time.strftime("%Y-%m-%d", time.localtime(arr[-1].get("x", 0) / 1000))
                if len(arr) > 21:
                    out["近20日净值变动%"] = round(
                        (arr[-1].get("y", 0) / arr[-21].get("y", 1) - 1) * 100, 2)
        m = re.search(r"Data_fluctuationScale\s*=\s*(\{.*?\})\s*;", js, re.S)
        if m:
            d = json.loads(m.group(1))
            series = d.get("series") or []
            if series:
                out["最新规模(亿元)"] = series[-1].get("y")
                out["规模环比"] = series[-2].get("y") if len(series) > 1 else None
    except Exception:
        pass
    return out


_ISSUER = re.compile(
    r"(银华|华夏|易方达|广发|南方|嘉实|汇添富|富国|招商|工银|天弘|华宝|国泰|博时|鹏华|"
    r"景顺长城|摩根|中欧|华安|大成|万家|平安|兴业|民生|建信|交银|永赢|国联安|中银|浦银|"
    r"西部利得|华泰柏瑞|中信保诚|德邦|东财|方正|长城|国投|银河|泰康|人保|太平|新华|融通|"
    r"前海|东方|中信建投|申万|财通|浙商|兴证|长江|华商|信达澳亚|金鹰|诺安|长盛|宝盈|"
    r"上投摩根|农银|中金|国寿安保|安信|中加|鑫元|弘毅远方)$")


def _theme_of(name, code):
    """由基金全名推出行业 / 主题词，用于抓取更宏观的行业与政策面变化。"""
    s = name or ""
    s = re.sub(r"[（(].*?[)）]", "", s)
    s = re.sub(r"(ETF|LOF|联接|基金|指数|交易型开放式)$", "", s)
    s = _ISSUER.sub("", s.strip())
    s = re.sub(r"(ETF|LOF|联接|基金)$", "", s).strip()
    return s if len(s) >= 2 else ""


_MKT_PREFIX = re.compile(
    r"^(港股通|港股|沪港深|恒生|中概|纳斯达克|标普|A股|深证|中证|上证|创业板|科创板|"
    r"全球|海外|MSCI|中盘|小盘|大盘|央企|国企|行业)")


def _core_of(theme):
    """主题词的核心行业词：港股创新药 → 创新药，用于过滤检索噪声。"""
    s = _MKT_PREFIX.sub("", theme or "").strip()
    return s if len(s) >= 2 else (theme or "")


_ALIAS = (
    (("药", "医"), ("医药", "生物医药", "创新药", "医疗器械")),
    (("半导体", "芯片"), ("半导体", "芯片", "集成电路")),
    (("新能源",), ("锂电", "光伏", "储能", "新能源")),
    (("军工",), ("军工", "国防")),
    (("消费",), ("消费", "白酒", "食品饮料")),
    (("证券", "券商"), ("证券", "券商", "非银")),
    (("人工智能", "AI"), ("人工智能", "算力", "AI")),
    (("有色",), ("有色", "黄金", "稀土", "铜")),
)


def _alias(core):
    """核心行业词的同义词集合，用于检索结果相关性判定。"""
    out = {core} if core else set()
    for keys, words in _ALIAS:
        if any(k in core for k in keys):
            out |= set(words)
    return out


def _kline_chg(symbol, n=5, timeout=12):
    """取近 n 个交易日涨跌幅（%）：腾讯日K，symbol 形如 sh000001 / hkHSI。

    东财 push2his 在本机被拒，腾讯 web.ifzq.gtimg.cn 稳定可用。
    日K行格式 [日期, 开, 收, 高, 低, 量]，取首尾收盘价。
    """
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
           % (symbol, n + 1))
    try:
        js = json.loads(_http_get(url, timeout=timeout, referer="https://gu.qq.com/"))
    except Exception:
        return None
    node = ((js.get("data") or {}).get(symbol)) or {}
    rows = node.get("day") or node.get("qfqday") or []
    if len(rows) < 2:
        return None
    try:
        a, b = float(rows[0][2]), float(rows[-1][2])
    except Exception:
        return None
    if not a:
        return None
    return round((b / a - 1) * 100, 2)


def fetch_market_env(market="", timeout=12):
    """大盘 / 所属市场环境：恒生指数 + 上证指数近 5 日涨跌（港股标的给恒指加倍权重）。"""
    out = {}
    for label, sym in (("恒生指数", "hkHSI"), ("上证指数", "sh000001")):
        c = _kline_chg(sym, timeout=timeout)
        if c is not None:
            out[label + "近5日%"] = c
    if not out:
        return {}
    vals = list(out.values())
    if "恒生指数近5日%" in out:
        vals.append(out["恒生指数近5日%"])
    out["环境分"] = round(math.tanh((sum(vals) / len(vals)) / 5.0), 3)
    return out


# ---------------------------------------------------------------- 主评估
def evaluate(code, name="", market="", fund=None, force=False, timeout=12):
    """评估标的的基本面 + 突发事件，返回参数字典（含缓存与 stale 标注）。"""
    code = str(code or "").strip()
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_fp = os.path.join(CACHE_DIR, "events_%s.json" % (code or "unknown"))

    if not force and os.path.exists(cache_fp):
        try:
            with open(cache_fp, encoding="utf-8") as f:
                cached = json.load(f)
            if time.time() - cached.get("_ts", 0) < CACHE_TTL:
                cached["cached"] = True
                return cached
        except Exception:
            pass

    news, err = [], None
    theme = _theme_of(name, code)
    core = _core_of(theme)
    queries = [q for q in ([code] if code else []) + ([theme] if theme else []) if q]
    seen = set()
    try:
        for q in queries:
            strict = _alias(core) if (core and q != code) else None   # 主题词检索按标题判相关性
            for n in fetch_news(q, size=12, timeout=timeout):
                k = n["title"]
                if not k or k in seen:
                    continue
                if strict is not None and not (
                        any(t in k for t in strict) or (code and code in k)):
                    continue
                seen.add(k)
                news.append(n)
        news.sort(key=lambda x: x["date"], reverse=True)
        news = news[:16]
        if not news and theme:
            # 严格过滤后为空（标题未含行业同义词）→ 放宽为主题词命中即收录，避免外部参数退化为全中性
            for n in fetch_news(theme, size=12, timeout=timeout):
                k = n.get("title")
                if not k or k in seen:
                    continue
                seen.add(k)
                news.append(n)
            news.sort(key=lambda x: x["date"], reverse=True)
            news = news[:12]
    except Exception as e:
        err = "%s" % e

    env = {}
    try:
        env = fetch_market_env(market, timeout=timeout)
    except Exception:
        env = {}

    scored, tot_w, acc = [], 0.0, 0.0
    for n in news:
        text = (n["title"] + " " + n["content"]).strip()
        if not text:
            continue
        s, hits = _score_text(text)
        cat, cat_w = _category(text)
        tw = _age_weight(n["date"])
        w = tw * cat_w
        s_norm = math.tanh(s / 3.0)                    # 单条 -1 ~ 1
        acc += s_norm * w
        tot_w += w
        n.update({"情绪": round(s_norm, 2), "类别": cat, "命中词": [h[0] for h in hits][:6]})
        scored.append(n)

    sentiment = round(acc / tot_w, 3) if tot_w > 0 else 0.0
    strong = sum(1 for n in scored if abs(n["情绪"]) >= 0.6 and _age_weight(n["date"]) >= 0.85)
    impact = "high" if strong >= 3 else ("medium" if strong >= 1 else "low")

    basis = {}
    for src in (fund or {}, fetch_fund_basis(code) if code.isdigit() else {}):
        for k, v in (src or {}).items():
            if v not in (None, "", "暂不可用") and k not in basis:
                basis[k] = v
    bs = 0.0
    prem = basis.get("折溢价%")
    if isinstance(prem, (int, float)):
        bs += max(-1.0, min(1.0, -prem / 1.5))         # 高溢价 = 风险
    chg = basis.get("近20日净值变动%")
    if isinstance(chg, (int, float)):
        bs += max(-1.0, min(1.0, chg / 8.0))
    basis_score = round(max(-1.0, min(1.0, bs / 2.0)), 3) if basis else 0.0

    env_score = env.get("环境分", 0.0)
    # 综合外部环境分：事件面 55% + 基本面 25% + 大盘环境 20%
    combined = round(max(-1.0, min(1.0,
                                    0.55 * sentiment + 0.25 * basis_score + 0.20 * env_score)), 3)

    # ---- 修正系数：负面 → 收紧止损 / 下调目标 / 抬高买入门槛
    s = combined
    adjust = {
        "stop_shift_atr": round(0.35 * max(0.0, -s) - 0.10 * max(0.0, s), 3),
        "target_shift_atr": round(-0.60 * max(0.0, -s) + 0.50 * max(0.0, s), 3),
        "buy_gate_mult": round(min(2.2, max(0.55, 1.0 + 0.7 * max(0.0, -s) - 0.4 * max(0.0, s))), 3),
        "sell_first": bool(s < -0.15),
        "impact": impact,
    }

    out = {
        "ok": True,
        "code": code,
        "name": name,
        "as_of": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source": "东方财富全文检索",
        "sentiment": sentiment,
        "impact": impact,
        "basis_score": basis_score,
        "env_score": env_score,
        "combined": combined,
        "env": env,
        "theme": theme,
        "adjust": adjust,
        "basis": basis,
        "events": scored[:8],
        "news_total": len(scored),
        "error": err,
        "_ts": time.time(),
        "cached": False,
        "stale": False,
    }
    if news:
        try:
            with open(cache_fp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
        except Exception:
            pass
    elif os.path.exists(cache_fp):
        try:
            with open(cache_fp, encoding="utf-8") as f:
                old = json.load(f)
            old.update({"cached": True, "stale": True})
            return old
        except Exception:
            pass
    return out


if __name__ == "__main__":
    import sys
    cd = sys.argv[1] if len(sys.argv) > 1 else "159567"
    r = evaluate(cd, name="港股创新药ETF银华", force=True)
    print(json.dumps({k: v for k, v in r.items() if k != "events"}, ensure_ascii=False, indent=2))
    for e in r["events"]:
        print(" -", e["date"], "|", e["情绪"], "|", e["类别"], "|", e["title"][:56])
