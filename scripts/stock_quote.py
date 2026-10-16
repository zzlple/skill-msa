# -*- coding: utf-8 -*-
"""
A股实时行情双源封装：腾讯(qt.gtimg.cn) 优先 + 新浪(hq.sinajs.cn) 备用，自动故障切换。

特点：
- 零第三方依赖（仅标准库 urllib）
- 腾讯源失败/超时/解析异常时自动切换新浪源，无需人工干预
- 源健康状态管理：连续失败 N 次标记 down，冷却 cooldown 秒后自动重试恢复
- 统一返回 Quote 数据结构；支持单只/批量查询
- 仅供个人学习使用：网页公开端点无 SLA，可能限频/改版/封 IP

用法：
    from stock_quote import StockQuoteClient
    c = StockQuoteClient()
    q = c.get("600519")                  # 单只，返回 Quote
    qs = c.get_many(["600519", "sz000001"])  # 批量
    q.dict() / q.print()                 # 查看结果
"""
import json
import socket
import ssl
import time
import urllib.request
from dataclasses import dataclass, field, asdict

DEFAULT_TIMEOUT = 3.0          # 单次请求超时（秒）
MAX_CONSECUTIVE_FAILS = 3      # 连续失败几次后标记源不可用
COOLDOWN_SECONDS = 60          # 源被标记 down 后的冷却时间（秒）
PREFERRED_SOURCE = "tencent"   # 首选源


# ------------------------- 代码归一化 -------------------------
def normalize_symbol(symbol: str) -> str:
    """把各种写法统一为 'sh600519' / 'sz000001' / 'bj430047' 格式。

    支持：600519 / sh600519 / 600519.SH / 000001.SZ / 600519.sh
    """
    s = str(symbol).strip().lower().replace(" ", "")
    if "." in s:  # 600519.SH / 000001.SZ
        code, market = s.split(".", 1)
        if market.startswith("sh"):
            return f"sh{code}"
        if market.startswith("sz"):
            return f"sz{code}"
        if market.startswith("bj"):
            return f"bj{code}"
        return s
    if s[:2] in ("sh", "sz", "bj") and len(s) == 8:
        return s
    if s[:2] in ("sh", "sz", "bj") and len(s) > 2:  # 带前缀但长度异常
        raise ValueError(f"无法识别的代码格式: {symbol}")
    if s.startswith(("60", "68", "51", "58", "56", "50", "88")):      # 沪市主板/科创板/沪基金
        return f"sh{s}"
    if s.startswith(("00", "30", "12", "15", "16", "18", "20")):      # 深市主板/创业板/深基金
        return f"sz{s}"
    if s.startswith(("43", "82", "83", "87", "88", "92")):            # 北交所
        return f"bj{s}"
    raise ValueError(f"无法识别的代码格式: {symbol}（示例: 600519 / sh600519 / 600519.SH）")


# ------------------------- 统一数据结构 -------------------------
@dataclass
class Quote:
    symbol: str          # 归一化代码，如 sh600519
    code: str            # 6 位代码
    name: str            # 股票名称
    price: float         # 最新价
    prev_close: float    # 昨收
    open: float          # 今开
    high: float          # 最高
    low: float           # 最低
    volume: float        # 成交量（股）
    amount: float        # 成交额（元）
    bid: list = field(default_factory=list)   # [(买一价,买一量), ... 买五]
    ask: list = field(default_factory=list)   # [(卖一价,卖一量), ... 卖五]
    change: float = 0.0     # 涨跌额
    change_pct: float = 0.0 # 涨跌幅 %
    datetime: str = ""      # 行情时间
    source: str = ""        # 实际命中的源: tencent / sina

    def dict(self):
        return asdict(self)

    def print(self):
        print(f"[{self.code}] {self.name}  最新 {self.price}  涨跌 {self.change:+.2f} ({self.change_pct:+.2f}%)")
        print(f"    今开 {self.open}  最高 {self.high}  最低 {self.low}  昨收 {self.prev_close}")
        print(f"    成交量 {self.volume:.0f} 股  成交额 {self.amount:.0f} 元  时间 {self.datetime}")
        print(f"    买五 {self.bid}  卖五 {self.ask}  来源 {self.source}")


def _to_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


# ------------------------- 源 1：腾讯 -------------------------
def tencent_fetch(symbols, timeout=DEFAULT_TIMEOUT) -> dict:
    """请求腾讯行情，返回 {symbol: 字段列表}。失败抛异常由上层切换。"""
    url = "http://qt.gtimg.cn/q=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://gu.qq.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("gbk", errors="ignore")
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, payload = line.split("=", 1)
        if payload.strip() in ('""', ''):
            continue
        sym = var.replace("v_", "").strip()
        f = payload.strip().strip('"').split("~")
        out[sym] = f
    if not out:
        raise RuntimeError("腾讯接口返回为空")
    return out


def tencent_parse(fields) -> Quote:
    if len(fields) < 45:
        raise ValueError(f"腾讯返回字段不足: {len(fields)}")
    f = fields
    bid = [(f[9 + i * 2], f[10 + i * 2]) for i in range(5)]
    ask = [(f[19 + i * 2], f[20 + i * 2]) for i in range(5)]
    return Quote(
        symbol=f[2], code=f[2], name=f[1],
        price=_to_float(f[3]), prev_close=_to_float(f[4]), open=_to_float(f[5]),
        high=_to_float(f[33]), low=_to_float(f[34]),
        volume=_to_float(f[36]) * 100 if f[36] else 0.0,   # 腾讯成交量单位：手 -> 股
        amount=_to_float(f[37]) * 10000,                    # 成交额单位：万元 -> 元
        bid=[(_to_float(p), _to_float(v) * 100) for p, v in bid],
        ask=[(_to_float(p), _to_float(v) * 100) for p, v in ask],
        change=_to_float(f[31]), change_pct=_to_float(f[32]),
        datetime=_fmt_time(f[30]), source="tencent",
    )


# ------------------------- 源 2：新浪 -------------------------
def sina_fetch(symbols, timeout=DEFAULT_TIMEOUT) -> dict:
    """请求新浪行情，返回 {symbol: 字段列表}。注意必须带 Referer，否则 403。"""
    url = "https://hq.sinajs.cn/list=" + ",".join(symbols)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": "https://finance.sina.com.cn/",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read().decode("gbk", errors="ignore")
    out = {}
    for line in raw.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        var, payload = line.split("=", 1)
        sym = var.replace("var hq_str_", "").strip()
        f = payload.strip().strip('";').split(",")
        out[sym] = f
    if not out:
        raise RuntimeError("新浪接口返回为空")
    return out


def sina_parse(fields) -> Quote:
    if len(fields) < 32:
        raise ValueError(f"新浪返回字段不足: {len(fields)}")
    f = fields
    price, prev = _to_float(f[3]), _to_float(f[2])
    bid = [(f[11 + i * 2], f[10 + i * 2]) for i in range(5)]   # 新浪: 量在前,价在后
    ask = [(f[21 + i * 2], f[20 + i * 2]) for i in range(5)]
    return Quote(
        symbol="", code="", name=f[0],
        price=price, prev_close=prev, open=_to_float(f[1]),
        high=_to_float(f[4]), low=_to_float(f[5]),
        volume=_to_float(f[8]), amount=_to_float(f[9]),
        bid=[(_to_float(p), _to_float(v)) for p, v in bid],
        ask=[(_to_float(p), _to_float(v)) for p, v in ask],
        change=price - prev if prev else 0.0,
        change_pct=(price - prev) / prev * 100 if prev else 0.0,
        datetime=f"{f[30]} {f[31]}", source="sina",
    )


def _fmt_time(t: str) -> str:
    """腾讯时间格式 20260916103000 -> 2026-09-16 10:30:00；空值原样返回。"""
    t = t.strip()
    if len(t) == 14 and t.isdigit():
        return f"{t[0:4]}-{t[4:6]}-{t[6:8]} {t[8:10]}:{t[10:12]}:{t[12:14]}"
    return t


# ------------------------- 双源调度客户端 -------------------------
SOURCES = {"tencent": (tencent_fetch, tencent_parse), "sina": (sina_fetch, sina_parse)}


class StockQuoteClient:
    """双源自动切换客户端。

    调度策略：
    1. 默认优先腾讯；腾讯连续失败 MAX_CONSECUTIVE_FAILS 次 -> 标记 down 并冷却 COOLDOWN_SECONDS；
       冷却期内请求直接走新浪；冷却结束后自动用腾讯重试，成功即恢复首选。
    2. 新浪同理，只是它作为备用源被标记后，仅当腾讯也不可用时才临时尝试。
    """

    def __init__(self, preferred=PREFERRED_SOURCE, timeout=DEFAULT_TIMEOUT,
                 max_fails=MAX_CONSECUTIVE_FAILS, cooldown=COOLDOWN_SECONDS):
        assert preferred in SOURCES, f"preferred 必须是 {list(SOURCES)}"
        self.preferred = preferred
        self.timeout = timeout
        self.max_fails = max_fails
        self.cooldown = cooldown
        self._fails = {s: 0 for s in SOURCES}
        self._down_until = {s: 0.0 for s in SOURCES}

    # ---- 对外接口 ----
    def get(self, symbol) -> Quote:
        """查询单只股票，返回 Quote。"""
        return self.get_many([symbol])[0]

    def get_many(self, symbols) -> list:
        """批量查询，返回与输入同序的 Quote 列表；整体失败时抛异常。"""
        norm = [normalize_symbol(s) for s in symbols]
        order = SOURCES["tencent" if self.preferred == "tencent" else "sina"]  # 保留
        # 按源调度（保留入参顺序：查询和解析都按传入顺序处理）
        source = self._pick_source()
        try:
            raw = SOURCES[source][0](norm, self.timeout)
            quotes = [self._parse_one(source, raw, sym) for sym in norm]
            self._mark_ok(source)
            return quotes
        except Exception as e:
            self._mark_fail(source)
            alt = self._other(source)
            try:
                raw = SOURCES[alt][0](norm, self.timeout)
                quotes = [self._parse_one(alt, raw, sym) for sym in norm]
                self._mark_ok(alt)
                return quotes
            except Exception as e2:
                raise RuntimeError(f"双源均失败: {source} -> {e}; {alt} -> {e2}") from e2

    # ---- 内部调度 ----
    def _pick_source(self) -> str:
        """按健康状态选源：首选源可用则用它，否则用备用源。"""
        if self._source_ok(self.preferred):
            return self.preferred
        return self._other(self.preferred)

    def _source_ok(self, name: str) -> bool:
        return time.time() >= self._down_until[name]

    def _mark_ok(self, name: str) -> None:
        self._fails[name] = 0

    def _mark_fail(self, name: str) -> None:
        self._fails[name] += 1
        if self._fails[name] >= self.max_fails:
            self._down_until[name] = time.time() + self.cooldown
            print(f"[stock_quote] 源 {name} 连续失败 {self.max_fails} 次，"
                  f"冷却 {self.cooldown}s（到 {time.strftime('%H:%M:%S', time.localtime(self._down_until[name]))}）")
        if name == self.preferred:
            print(f"[stock_quote] 首选源 {name} 本次失败，已切换备用源")

    @staticmethod
    def _other(name: str) -> str:
        return "sina" if name == "tencent" else "tencent"

    @staticmethod
    def _parse_one(source: str, raw: dict, sym: str) -> Quote:
        if sym not in raw:
            raise ValueError(f"{source} 未返回 {sym} 的数据")
        q = SOURCES[source][1](raw[sym])
        q.symbol = sym
        if not q.code:
            q.code = sym[2:]
        return q


# ------------------------- 演示与自测 -------------------------
if __name__ == "__main__":
    import sys

    client = StockQuoteClient()

    if "--test-switch" in sys.argv:
        # 自测：强制首选源为一个必定失败的假源来验证切换逻辑
        print("== 自测：模拟首选源不可用，验证自动切换 ==")
        c2 = StockQuoteClient(preferred="sina")
        c2.preferred = "tencent"
        # 直接把腾讯源替换成抛异常的假源
        SOURCES["tencent"] = (lambda *a, **k: (_ for _ in ()).throw(RuntimeError("模拟腾讯接口故障")), tencent_parse)
        try:
            q = c2.get("600519")
            q.print()
            print("切换成功，备用源可用 ✓")
        finally:
            SOURCES["tencent"] = (tencent_fetch, tencent_parse)
        sys.exit(0)

    symbols = sys.argv[1:] or ["600519", "sz000001", "300750"]
    print(f"== 查询 {symbols} ==")
    for q in client.get_many(symbols):
        q.print()
