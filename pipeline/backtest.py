"""回测：指数到底有没有提前量？

三个指标，缺一不可：
  * **提前量** lead_days —— 在事件发生前多少天越过了阈值。没有提前量的
    指数只是新闻摘要，不配叫预警。
  * **误报率** fp_rate —— 在事件发生前那段平静期里，有多少天本来就
    越过了阈值。太高说明阈值太松，天天报红。
  * **事件后余波** peak_post —— 事后暴涨是"复述新闻"，不是预测。

诚实的前提：没有历史数据就没有回测。数据不够时本模块返回
`status="no_data"` 并说明缺多少天，**绝不编造一条漂亮的结论**。
要跑回测，先 `python -m pipeline.cli backfill --days 400`。
"""
from __future__ import annotations

from datetime import date, timedelta

from . import build
from .store import Series

# 锚点：公认"局势明显升级"的日期。选它们的理由是可争议性低——
# 如果指数在这些日子前后毫无反应，那它测的就不是升级。
ANCHORS: list[tuple[str, date, set[str], str]] = [
    ("2022 俄乌开战", date(2022, 2, 24), {"RUSSIA_CIS", "EUROPE"}, "全面入侵，二战后欧洲最大战争"),
    ("2023 加沙战争", date(2023, 10, 7), {"MIDDLE_EAST"}, "哈马斯突袭，地区全面升级"),
    ("2024 伊以互袭", date(2024, 4, 13), {"MIDDLE_EAST"}, "首次国家间直接导弹互射"),
    ("2020 纳卡战争", date(2020, 9, 27), {"RUSSIA_CIS"}, "高加索冻结冲突重启"),
    ("2014 克里米亚", date(2014, 3, 18), {"RUSSIA_CIS", "EUROPE"}, "吞并克里米亚"),
    ("2024 朝韩紧张", date(2024, 10, 15), {"EAST_ASIA"}, "炸毁连接道路、边境射击"),
]

LEAD_WINDOW = 30      # 事件前多少天算"提前预警窗口"
BASELINE_DAYS = 90    # 平静期基线长度
AFTER_DAYS = 7        # 事后观察长度
QUANTILE = 0.85       # 阈值取平静期的 85 分位


def _quantile(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def run_anchor(series: Series, anchor: date, regions: set[str],
               name: str = "", note: str = "") -> dict:
    start = anchor - timedelta(days=BASELINE_DAYS + LEAD_WINDOW)
    end = anchor + timedelta(days=AFTER_DAYS)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    results = build.compute_window(series, days, sorted(regions)) \
        if _window_sig_ok(series, sorted(regions), days) else {}
    if not results:
        return {"name": name, "anchor": anchor.isoformat(), "status": "no_data",
                "note": note, "missing_days": len(days)}

    values, _ = build.index_series(results, days)
    curve = {d: v for d, v in zip(days, values)}

    # 数据够不够：至少一半的日子真有区域读数（而不是靠补零算出来的 50 分）
    covered = sum(1 for r in results.values() if r.regions)
    if covered < len(days) * 0.6:
        return {"name": name, "anchor": anchor.isoformat(), "status": "no_data",
                "note": note, "covered_days": covered, "total_days": len(days)}

    baseline_days = [d for d in days if d < anchor - timedelta(days=LEAD_WINDOW)]
    baseline = [curve[d] for d in baseline_days]
    threshold = _quantile(baseline, QUANTILE)

    pre_days = [d for d in days if anchor - timedelta(days=LEAD_WINDOW) <= d < anchor]
    hits = [d for d in pre_days if curve[d] >= threshold]
    lead = (anchor - hits[0]).days if hits else 0

    peak_pre_day = max(pre_days, key=lambda d: curve[d]) if pre_days else None
    post_days = [d for d in days if d >= anchor]
    peak_post_day = max(post_days, key=lambda d: curve[d]) if post_days else None

    fp = sum(1 for d in baseline_days if curve[d] >= threshold)
    fp_rate = fp / len(baseline_days) if baseline_days else 0.0

    return {
        "name": name,
        "anchor": anchor.isoformat(),
        "note": note,
        "status": "ok",
        "threshold": round(threshold, 1),
        "lead_days": lead,
        "warned": bool(hits),
        "peak_pre": round(curve[peak_pre_day], 1) if peak_pre_day else None,
        "peak_pre_day": peak_pre_day.isoformat() if peak_pre_day else None,
        "peak_post": round(curve[peak_post_day], 1) if peak_post_day else None,
        "peak_post_day": peak_post_day.isoformat() if peak_post_day else None,
        "fp_rate": round(fp_rate, 3),
        "covered_days": covered,
        "total_days": len(days),
    }


def _window_sig_ok(series: Series, regions: list[str], days: list[date]) -> bool:
    """索引里根本没有这些区域的数据时，别浪费时间去算。"""
    have = {r for (r, _c) in series}
    return any(reg in have for reg in regions)


def run_all(series: Series) -> list[dict]:
    return [run_anchor(series, a, regs, name, note) for name, a, regs, note in ANCHORS]


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if r.get("status") == "ok"]
    if not ok:
        return {
            "status": "no_data",
            "ran": 0,
            "verdict": "还没有足够的历史数据，回测未运行。先跑 backfill 回补，"
                       "否则任何「准确率」都只能是编的。",
            "rows": rows,
        }
    leads = [r["lead_days"] for r in ok]
    warned = [r for r in ok if r["warned"]]
    fps = [r["fp_rate"] for r in ok]
    leads.sort()
    median_lead = leads[len(leads) // 2]
    return {
        "status": "ok",
        "ran": len(ok),
        "warned": len(warned),
        "median_lead_days": median_lead,
        "fp_rate_mean": round(sum(fps) / len(fps), 3),
        "verdict": _verdict(len(ok), len(warned), median_lead, sum(fps) / len(fps)),
        "rows": rows,
    }


def _verdict(ran: int, warned: int, median_lead: int, fp: float) -> str:
    if ran < 3:
        return f"只回测了 {ran} 个锚点，样本太小，结论不可用。"
    if warned / ran < 0.5:
        return (f"{ran} 个锚点里只有 {warned} 个在事前 30 天内越过阈值——"
                f"漏报率高，说明当前权重对慢变量的捕捉不足，或者阈值定得太高。")
    if fp > 0.35:
        return (f"提前量中位数 {median_lead} 天，但平静期误报率 {fp:.0%} 偏高，"
                f"属于「天天喊狼来了」——需要收紧分项权重或提高阈值。")
    return (f"{ran} 个锚点中 {warned} 个提前预警，中位提前量 {median_lead} 天，"
            f"平静期误报率 {fp:.0%}。可用，但样本仍偏少，别据此下重注。")
