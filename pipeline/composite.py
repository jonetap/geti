"""子指数与总指数（末日指数）。

设计取舍必须先说清楚，否则这个数字会变成吓人的玩具：

1. **三个子指数用的是同一种语言**：0–100，50 = 各自过去 90 天的中位数。
   所以「灾害 60」和「军事 60」是同一个意思——比平时高出一点。
2. **末日指数不是三者平均**。真正的末日不是"三件小事"，而是"至少两件大事
   同时发生"（多重危机）。所以用 `max` 与加权均值各占一半，再加一条共振加成：
   两个以上子系统同时越过 0.8σ，额外加分。三个都平庸，分数上不去。
3. **缺数据时不许装**。灾害和疫情的历史是逐日累积的，头两周分项为 null，
   此时末日指数只由军事一项构成，必须标 `partial`，页面要写"数据累积中"。

反过来说，这个指数不能用来预测末日——它只回答"今天这个世界比平时更不正常吗，
如果不正常，是几个方向同时不正常"。仍然是体温计。
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from . import epidemic, hazard, score

SUBI = {
    "hazard": (hazard.WEIGHTS, hazard.LABELS),
    "epidemic": (epidemic.WEIGHTS, epidemic.LABELS),
}
MIN_HISTORY = score.MIN_HISTORY_DAYS   # 与主指数同一门槛（14 天），两套标准会让人对不上账
TANH_K = 1.2
RESO_THRESHOLD = 0.8     # 共振门槛（z 单位）
RESO_BONUS = 0.35        # 每多一个子系统越线，加多少 z

# 灾害与疫情的**影响不会当天结束**：一场疫情爆发后，新闻可能第二天就没了，
# 但病人还在、余震还在、救援还在。所以子指数算的不是"今天有多少条"，
# 而是"按 7 天半衰期加权之后，现在的负担有多重"。
# 主指数（军事）不用这套——外交事件是离散的，昨天的会谈谈崩了不代表今天还在崩。
HALF_LIFE_DAYS = 7.0
SMOOTH_SPAN = 28         # 再往前权重已 <6%，截断省算力


def _smooth(cell: dict, day: date, first: date) -> float | None:
    """7 天半衰期加权平均。缺失日按 0 计（没报告 = 没发生），
    但 first 之前不参与（那天还没开始采集，不是"当天为零"）。"""
    total = 0.0
    wsum = 0.0
    for k in range(SMOOTH_SPAN + 1):
        d = day - timedelta(days=k)
        if d < first:
            break
        w = 0.5 ** (k / HALF_LIFE_DAYS)
        total += w * cell.get(d, 0.0)
        wsum += w
    return (total / wsum) if wsum > 0 else None


def persistence(series: dict, kind: str, end: date, span: int = 14) -> dict:
    """近 span 天里有多少天"有东西在发生"——用来回答"这事过去了没有"。"""
    comps = SUBI[kind][0]
    active = 0
    last = None
    for i in range(span):
        d = end - timedelta(days=i)
        s = sum(series.get((kind, c), {}).get(d, 0.0) for c in comps)
        if s > 0:
            active += 1
            if last is None:
                last = d
    return {"span": span, "active_days": active,
            "last_seen": last.isoformat() if last else None,
            "ongoing": active >= 3}


def readings_to_series(kind: str, day: date, readings: dict) -> dict:
    """把一次抓取结果摊成 Series 片段（和 store 同构）。"""
    if kind not in SUBI:
        raise ValueError(f"未知子指数 {kind}")
    return {(kind, k): {day: float(v)} for k, v in readings.items()}


def subindex(series: dict, kind: str, end: date, days: int = 90) -> dict:
    """算某一天的子指数。历史不足的分项直接缺席，不补零。"""
    weights, labels = SUBI[kind]
    start = end - timedelta(days=days)
    rows = []
    zsum = 0.0
    wsum = 0.0
    hist_days = 0
    for comp, w in weights.items():
        cell = series.get((kind, comp), {})
        first = min(cell) if cell else None
        raw_today = cell.get(end)
        hist_raw = [v for d, v in cell.items() if start <= d < end]
        hist_days = max(hist_days, len(hist_raw))
        today = _smooth(cell, end, first) if first else None
        if today is None or len(hist_raw) < MIN_HISTORY:
            rows.append({"key": comp, "label": labels[comp], "weight": w,
                         "value": None if raw_today is None else round(raw_today, 2),
                         "carry": None if today is None else round(today, 2),
                         "z": None, "score": None, "available": False})
            continue
        hist = []
        for i in range(1, days + 1):
            d = end - timedelta(days=i)
            if d < first:
                break
            v = _smooth(cell, d, first)
            if v is not None:
                hist.append(v)
        z = score.robust_z(today, hist)
        if z is None:
            rows.append({"key": comp, "label": labels[comp], "weight": w,
                         "value": round(raw_today, 2),
                         "carry": round(today, 2), "z": None, "score": None,
                         "available": False})
            continue
        zsum += w * z
        wsum += w
        rows.append({"key": comp, "label": labels[comp], "weight": w,
                     "value": round(raw_today, 2),
                     "carry": round(today, 2), "z": round(z, 2),
                     "score": round(score.to_index(z), 1), "available": True})

    if wsum <= 0:
        return {"index": None, "z": None, "components": rows,
                "history_days": hist_days, "components_ok": 0,
                "decay_days": HALF_LIFE_DAYS,
                "persistence": persistence(series, kind, end)}

    z_agg = zsum / wsum
    idx = score.to_index(z_agg) if abs(z_agg) > 1e-9 else 50.0
    return {"index": round(idx, 1), "z": round(z_agg, 2), "components": rows,
            "history_days": hist_days, "components_ok": sum(1 for r in rows if r["available"]),
            "decay_days": HALF_LIFE_DAYS,
            "persistence": persistence(series, kind, end)}


def _to_z(index: float | None) -> float | None:
    """把 0–100 指数换回 z，才能和别的子系统同尺度相加。"""
    if index is None:
        return None
    p = max(-0.999, min(0.999, (index - 50.0) / 50.0))
    return TANH_K * math.atanh(p)


def doom(geti: float | None, hazard_idx: float | None,
         epidemic_idx: float | None) -> dict:
    """总指数。返回 {index, z, parts, resonance, partial, available}。"""
    parts = {"military": _to_z(geti), "hazard": _to_z(hazard_idx),
             "epidemic": _to_z(epidemic_idx)}
    avail = {k: v for k, v in parts.items() if v is not None}
    if not avail:
        return {"index": None, "z": None, "parts": parts, "resonance": 0,
                "partial": True, "available": []}

    base_w = {"military": 0.50, "hazard": 0.30, "epidemic": 0.20}
    wsum = sum(base_w[k] for k in avail) or 1.0
    mean_z = sum(base_w[k] * v for k, v in avail.items()) / wsum
    peak_z = max(avail.values())

    # max 与均值的混和：单点尖峰能推高，但不至于三个都平庸也报末日
    z = 0.55 * peak_z + 0.45 * mean_z

    resonance = sum(1 for v in avail.values() if v > RESO_THRESHOLD)
    if resonance >= 2:
        z += RESO_BONUS * (resonance - 1)

    idx = 50.0 + 50.0 * math.tanh(z / TANH_K)
    return {
        "index": round(max(0.0, min(100.0, idx)), 1),
        "z": round(z, 2),
        "parts": {k: None if v is None else round(v, 2) for k, v in parts.items()},
        "resonance": resonance,
        "partial": len(avail) < 3,
        "available": sorted(avail),
    }
