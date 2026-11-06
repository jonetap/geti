"""L3 市场层：用 FRED 的免注册 CSV 接口取宏观压力指标。

为什么单独一只温度计、不并进 GETI：
  市场是"钱在说话"，新闻是"人在说话"。两者背离本身是最有信息量的状态
  （市场已经在定价、新闻还没反应）。把市场混进区域分解，只会让
  "为什么今天涨了" 解释不清，而且它没有区域属性——华尔街的恐慌
  不该给台海加分。

接口选型（都实测过）：
  * stooq.com        —— fetch failed，不通
  * Yahoo chart API  —— HTTP 403（要 cookie/crumb，且对大陆 IP 有提示）
  * FRED fredgraph.csv —— 可用，application/csv，**不需要注册、不需要 key**
"""
from __future__ import annotations

import csv
import io
import math
from datetime import date, timedelta

from .fetch import http_get
from .score import median, robust_z

FRED = ("https://fred.stlouisfed.org/graph/fredgraph.csv"
        "?id={sid}&cosd={start}&coed={end}")

# (内部 id, FRED series id, 显示名, 上涨是否代表压力)
INDICATORS: list[tuple[str, str, str, bool]] = [
    ("vix", "VIXCLS", "VIX 波动率", True),
    ("oil", "DCOILWTICO", "WTI 原油", True),
    ("hy", "BAMLH0A0HYM2", "高收益债利差", True),
    ("usd", "DTWEXBGS", "广义美元", False),
    ("ust", "DGS10", "美十年期", False),
]

COMPOSITE_IDS = ("vix", "oil", "hy")   # 只让这三项进综合压力分
WINDOW = 90                            # 与指数同口径的历史窗口

# 粮价：FRED 上这条路只有**月度**序列（IMF 商品价格），而且发布滞后约两个月。
# 所以它测的不是"今天涨了"，是"这半年饭变贵了多少"——结构性压力。
# 单独成块、单独标注滞后，绝不并入 GETI（和上面的市场层同一条理由）。
FOOD: list[tuple[str, str, str]] = [
    ("wheat", "PWHEAMTUSDM", "小麦"),
    ("corn", "PMAIZMTUSDM", "玉米"),
    ("rice", "PRICENPQUSDM", "大米"),
    ("sugar", "PSUGAISAUSDM", "糖"),
    ("food", "PFOODINDEXM", "食品价格指数"),
]
FOOD_WINDOW = 24                       # 月度序列用 24 个月做基准，日频的 90 天在这里太短
FOOD_COMPOSITE = ("wheat", "corn", "rice", "sugar")


def _to_date(s: str) -> date | None:
    try:
        return date.fromisoformat(s.strip()[:10])
    except ValueError:
        return None


def parse_fred(text: str) -> dict[date, float]:
    """FRED CSV 是 `observation_date,SERIES_ID`，缺值是 '.'。"""
    out: dict[date, float] = {}
    reader = csv.reader(io.StringIO(text))
    next(reader, None)                       # 表头
    for row in reader:
        if len(row) < 2:
            continue
        d = _to_date(row[0])
        v = row[1].strip()
        if d is None or not v or v == ".":
            continue
        try:
            out[d] = float(v)
        except ValueError:
            continue
    return out


def fetch_market(days: int = WINDOW + 60, end: date | None = None) -> dict:
    """返回给快照用的 market 块。任何一个指标失败都不影响其它。"""
    end = end or date.today()
    start = end - timedelta(days=days)
    raw: dict[str, dict[date, float]] = {}
    health: list[dict] = []

    for sid, code, label, _ in INDICATORS:
        rec = {"source": f"fred:{code}", "ok": False, "items": 0, "error": ""}
        try:
            body = http_get(
                FRED.format(sid=code, start=start.isoformat(), end=end.isoformat()),
                timeout=30,
            ).decode("utf-8", errors="replace")
            got = parse_fred(body)
            raw[sid] = got
            rec["items"] = len(got)
            rec["ok"] = bool(got)
            if not got:
                rec["error"] = "解析到 0 行"
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        health.append(rec)

    return build_block(raw, health)


def build_block(raw: dict[str, dict[date, float]], health: list[dict] | None = None) -> dict:
    """纯函数：把各指标的日值算成展示块。方便离线测试。"""
    rows: list[dict] = []
    zs: list[float] = []
    for sid, code, label, up_is_stress in INDICATORS:
        vals = raw.get(sid) or {}
        days = sorted(vals)
        if len(days) < 30:
            rows.append({"id": sid, "label": label, "last": None,
                         "chg5": None, "z": 0.0, "ok": False})
            continue
        last_day = days[-1]
        hist = [vals[d] for d in days[-WINDOW:]]
        z = robust_z(vals[last_day], hist)
        if z is None:
            z = 0.0
        if not up_is_stress:
            z = -z
        prev5 = vals.get(days[-6]) if len(days) >= 6 else None
        chg5 = ((vals[last_day] - prev5) / prev5 * 100.0) if prev5 else None
        rows.append({
            "id": sid, "label": label, "last": round(vals[last_day], 3),
            "chg5": round(chg5, 2) if chg5 is not None else None,
            "z": round(z, 3), "ok": True, "asof": last_day.isoformat(),
        })
        if sid in COMPOSITE_IDS:
            zs.append(z)

    # 综合：正 z = 压力高于常态
    index = 50.0
    z_mean = 0.0
    if zs:
        z_mean = sum(zs) / len(zs)
        index = 50.0 + 50.0 * math.tanh(z_mean / 1.2)

    return {
        "index": round(index, 1),
        "z": round(z_mean, 3),
        "rows": rows,
        "asof": max((r.get("asof") for r in rows if r.get("asof")), default=None),
        "source": "FRED (fredgraph.csv, 无需 key)",
        "health": health or [],
        "mode": "live" if zs else "empty",
    }


def median_level(rows: list[dict]) -> float | None:
    vals = [r["last"] for r in rows if r.get("last") is not None]
    return median(vals) if vals else None


# ---------------------------------------------------------------- 粮价（月度）

def fetch_food(months: int = FOOD_WINDOW + 12) -> dict:
    """月频数据，单独的 IO 路径。任何一项失败都不影响其它。"""
    start = date.today() - timedelta(days=int(months * 31))
    raw: dict[str, dict[date, float]] = {}
    health: list[dict] = []
    for sid, code, _ in FOOD:
        rec = {"source": f"fred:{code}", "ok": False, "items": 0, "error": ""}
        try:
            body = http_get(
                FRED.format(sid=code, start=start.isoformat(), end=date.today().isoformat()),
                timeout=30,
            ).decode("utf-8", errors="replace")
            got = parse_fred(body)
            raw[sid] = got
            rec["items"] = len(got)
            rec["ok"] = bool(got)
            if not got:
                rec["error"] = "解析到 0 行"
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        health.append(rec)
    return build_food_block(raw, health)


def build_food_block(raw: dict[str, dict[date, float]], health: list[dict] | None = None,
                     today: date | None = None) -> dict:
    """纯函数。z 是"相对于自己过去 24 个月"，不是相对于别的商品。"""
    today = today or date.today()
    rows: list[dict] = []
    zs: list[float] = []
    latest: date | None = None

    for sid, code, label in FOOD:
        vals = raw.get(sid) or {}
        days = sorted(vals)
        if len(days) < 6:
            rows.append({"id": sid, "label": label, "last": None,
                         "mom": None, "z": 0.0, "ok": False})
            continue
        last = days[-1]
        hist = [vals[d] for d in days[-FOOD_WINDOW:]]
        z = robust_z(vals[last], hist)
        z = 0.0 if z is None else z
        prev = days[-2]
        prev_v = vals.get(prev)
        mom = ((vals[last] - prev_v) / prev_v * 100.0) if prev_v else None
        rows.append({
            "id": sid, "label": label, "last": round(vals[last], 2),
            "mom": round(mom, 2) if mom is not None else None,
            "z": round(z, 2), "ok": True, "asof": last.isoformat()[:7],
        })
        if sid in FOOD_COMPOSITE:
            zs.append(z)
        latest = last if latest is None or last > latest else latest

    z_mean = sum(zs) / len(zs) if zs else 0.0
    index = 50.0 + 50.0 * math.tanh(z_mean / 1.2) if zs else 50.0
    lag = None
    if latest is not None:
        lag = (today.year - latest.year) * 12 + (today.month - latest.month)

    return {
        "index": round(index, 1),
        "z": round(z_mean, 3),
        "rows": rows,
        "asof": latest.isoformat() if latest else None,
        "lag_months": lag,
        "source": "FRED / IMF 商品价格（月度）",
        "health": health or [],
        "mode": "live" if zs else "empty",
    }
