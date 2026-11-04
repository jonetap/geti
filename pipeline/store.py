"""状态持久化：压力序列与快照。CI 是无状态的，所以历史必须落盘（git 提交）。"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Any

Series = dict[tuple[str, str], dict[date, float]]


def load_series(path: str) -> Series:
    out: Series = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                d = date.fromisoformat(rec["d"])
                out.setdefault((rec["r"], rec["c"]), {})[d] = float(rec["v"])
            except (ValueError, KeyError, TypeError):
                continue
    return out


def _merge(base: Series, new: Series) -> Series:
    out: Series = {}
    for src in (base, new):
        for k, days in src.items():
            out.setdefault(k, {}).update(days)
    return out


def save_series(path: str, new: Series, keep_days: int = 200, end: date | None = None,
                keep_existing: bool = False) -> Series:
    """把新算出的部分并进历史，覆盖同键同日（幂等），再整体重写。

    `keep_existing=True` 时**已存在的日期一律不再改写**。为什么要这个开关：
    每次运行都会重算最近两天，而 GDELT 的 36 小时窗口每 3 小时滚动一次，
    同一个日期在不同时刻会算出不同的值——于是历史序列会自己抖动，
    指数跟着一天掉 9 分再涨 10 分。那不是世界在变，是我们数了两遍。
    """
    base = load_series(path)
    if keep_existing:
        merged: Series = {k: dict(v) for k, v in base.items()}
        for k, days in new.items():
            slot = merged.setdefault(k, {})
            for d, v in days.items():
                slot.setdefault(d, v)
    else:
        merged = _merge(base, new)
    cutoff = (end or max((max(v) for v in merged.values() if v), default=date.today())) - timedelta(days=keep_days)

    lines: list[str] = []
    for (r, c), days in sorted(merged.items()):
        days = {d: v for d, v in days.items() if d >= cutoff}
        merged[(r, c)] = days
        for d, v in sorted(days.items()):
            lines.append(json.dumps({"r": r, "c": c, "d": d.isoformat(), "v": round(v, 4)},
                                    ensure_ascii=False, separators=(",", ":")))

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        fh.write("\n")
    os.replace(tmp, path)
    return merged


def write_json(path: str, obj: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
