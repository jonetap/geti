"""合成数据：离线验证整条链路，不联网、不烧 token。

这不是给用户看的"假数据"，而是测试夹具：
- 指数对"尖峰"是否有反应
- 多源转载是否被正确归并为 1 个起源
- 历史不足时是否拒绝给 z
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from .model import EventSignal, RawItem, cameo_component, region_of

UTC = timezone.utc

# 每个分项可选的 CAMEO 根码（模拟真实分布）
_CAMEO_BY_COMPONENT: dict[str, tuple[str, ...]] = {
    "military": ("19", "18", "17", "15", "13"),
    "diplomacy": ("16", "12", "11", "10", "05"),
    "economy": ("06", "07"),
    "internal": ("14", "09"),
    "narrative": ("01", "02", "03"),
    "physical": ("15", "16"),
}

_REGIONS = ("MIDDLE_EAST", "RUSSIA_CIS", "EAST_ASIA", "EUROPE", "SOUTH_ASIA", "AFRICA")
_FIPS = {
    "MIDDLE_EAST": "IS", "RUSSIA_CIS": "UP", "EAST_ASIA": "CH",
    "EUROPE": "PL", "SOUTH_ASIA": "IN", "AFRICA": "SU",
}


def synth_signals(
    days: int = 120,
    end: date | None = None,
    seed: int = 20261103,
    spike_day: int | None = None,
    spike_factor: float = 6.0,
) -> list[EventSignal]:
    """生成 days 天的事件流。spike_day 用于测试"某天爆发"能否被指数捕捉。"""
    rnd = random.Random(seed)
    end = end or date(2026, 11, 3)
    start = end - timedelta(days=days - 1)
    out: list[EventSignal] = []

    for i in range(days):
        day = start + timedelta(days=i)
        base = 4.0 + 0.004 * i                      # 极缓的趋势，避免指数被趋势顶满
        if spike_day is not None and i == days - 1 - spike_day:
            base *= spike_factor
        for reg in _REGIONS:
            # 中东基线更高
            mult = 1.6 if reg == "MIDDLE_EAST" else 1.0
            n = max(0, int(rnd.gauss(base * mult, 2.0)))
            for _ in range(n):
                comp = rnd.choices(
                    ["military", "physical", "diplomacy", "economy", "internal", "narrative"],
                    weights=[26, 22, 18, 15, 11, 8],
                )[0]
                code = rnd.choice(_CAMEO_BY_COMPONENT[comp]) + rnd.choice(["", "0", "1"])
                c_comp, sev = cameo_component(code)
                gold = -rnd.uniform(0.5, 9.0) if rnd.random() < 0.8 else rnd.uniform(1.0, 8.0)
                out.append(EventSignal(
                    day=day,
                    fips=_FIPS.get(reg, ""),
                    region=region_of(_FIPS.get(reg, "")),
                    cameo=code,
                    component=c_comp,
                    severity=sev,
                    goldstein=gold,
                    mentions=rnd.randint(1, 40),
                    actors=f"ACTOR_{reg} → TARGET_{reg}",
                    url=f"https://example.invalid/{day.isoformat()}/{rnd.randint(1, 99999)}",
                ))
    return out


_HEADLINES = [
    ("俄罗斯在乌克兰边境集结装甲部队，卫星图显示至少 200 辆坦克", "military", 5),
    ("以色列与黎巴嫩宣布暂停大使级外交关系并召回大使", "diplomacy", 4),
    ("韩国军方宣布在东海争议海域举行大规模联合演习", "military", 3),
    ("伊朗首都德黑兰爆发大规模抗议，政府宣布宵禁", "internal", 4),
    ("中国对关键矿产实施新的出口管制", "economy", 3),
    ("波兰防空系统在边境地区击落一架无人机", "military", 4),
    ("印度与巴基斯坦代表在第三国会谈，未达成任何协议", "diplomacy", 2),
]

_SOURCE_POOL = ("reuters", "ap", "afp", "bbc", "aljazeera", "isw", "telegram_osint", "x_osint")


def synth_items(days: int = 3, end: datetime | None = None, seed: int = 7) -> list[RawItem]:
    """生成带"多家转载同一事件"的新闻条目，用于验证起源归并。"""
    rnd = random.Random(seed)
    end = end or datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    out: list[RawItem] = []
    for d in range(days):
        for idx, (title, _comp, _sev) in enumerate(_HEADLINES):
            published = end - timedelta(days=d, hours=rnd.randint(0, 8))
            chosen = rnd.sample(_SOURCE_POOL, rnd.randint(1, 4))
            for sid in chosen:
                # 轻微改写标题，模拟不同媒体措辞
                variant = title if sid in ("reuters", "ap") else title + rnd.choice(
                    ["", "（更新）", "，多方证实", "——现场画面流出"])
                out.append(RawItem(
                    source_id=sid,
                    title=variant,
                    body=title + "。" + "细节待核实。" * rnd.randint(0, 3),
                    url=f"https://example.invalid/{sid}/{d}/{idx}",
                    published=published,
                ))
    return out
