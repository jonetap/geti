"""灾害层：地震 / 野火 / 风暴 / 火山 / 空间天气。

这一层只要"数值 + 图表 + 信源"，不做灾情研判。原则和主指数一致：
**每个源独立失败**，任何一个挂掉都不能影响其它源，也不能影响指数生成。
所有失败都写进 health，页面上看得见，不静默。

数据源全部免注册：
  * USGS FDSN 地震目录（可一次查一段日期，天然支持回补）
  * NASA EONET 自然灾害事件（野火/风暴/火山/洪水）
  * GDACS 全球灾害预警（红/橙/绿三级）
  * NOAA SWPC 地磁 Kp 指数
"""
from __future__ import annotations

import json
import math
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

from .fetch import _get_retry, on_day, parse_feed_items

UTC = timezone.utc

# 一天全球 4.5 级以上地震通常 10–40 次，取 4.5 已覆盖"有感觉"的量级
USGS_MIN_MAG = 4.5
USGS = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
        "&minmagnitude={mag}&starttime={s}&endtime={e}")
EONET = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days={days}"
GDACS = "https://www.gdacs.org/xml/rss.xml"
SWPC = [
    "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json",
]

# 分项权重：能量是主项（一次 8 级相当于一千次 6 级），数量用来区分"小震频发"
WEIGHTS = {
    "quake_energy": 0.34,
    "quake_count": 0.20,
    "storm_events": 0.20,
    "gdacs_alert": 0.16,
    "space_weather": 0.10,
}
LABELS = {
    "quake_energy": "地震能量",
    "quake_count": "强震次数",
    "storm_events": "灾害事件",
    "gdacs_alert": "红色预警",
    "space_weather": "地磁扰动",
}


def _iso_ms(ms) -> str:
    """USGS 的 time 是毫秒时间戳。"""
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).isoformat(timespec="minutes")
    except (TypeError, ValueError, OSError):
        return ""


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def energy_of(magnitudes) -> float:
    """把一组震级换成等效能量（焦耳的对数）。

    直接用次数会低估：一次 M7.8 释放的能量≈ 60 次 M6.0。用 10^(1.5M) 累加再取
    log10，量级会落在 12–20 之间，正好适合喂给 robust_z。
    """
    total = sum(10.0 ** (1.5 * m) for m in magnitudes) or 0.0
    return math.log10(total) if total > 0 else 0.0


def parse_usgs(text: str) -> dict:
    """USGS GeoJSON → 当日震级列表与统计。"""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    feats = doc.get("features") or []
    mags = [m for m in (_f((f.get("properties") or {}).get("mag")) for f in feats)
            if m is not None]
    items = []
    for f in feats:
        pr = f.get("properties") or {}
        m = _f(pr.get("mag"))
        if m is None:
            continue
        items.append({"t": str(pr.get("place") or "")[:80], "v": round(m, 1),
                      "ts": _iso_ms(pr.get("time")), "u": pr.get("url") or ""})
    items.sort(key=lambda x: -x["v"])
    return {
        "items": items[:10],
        "count": len(mags),
        "m6": sum(1 for m in mags if m >= 6.0),
        "max": max(mags) if mags else 0.0,
        "energy": energy_of(mags),
    }


def parse_eonet(text: str) -> dict:
    """NASA EONET → 按类别计数。"""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    cats: dict[str, int] = {}
    items = []
    for ev in doc.get("events") or []:
        geo = (ev.get("geometries") or [{}])[0]
        for c in ev.get("categories") or []:
            title = (c.get("title") or "other").lower()
            cats[title] = cats.get(title, 0) + 1
            items.append({"t": str(ev.get("title") or title)[:80], "v": title,
                          "ts": str(geo.get("date") or "")[:16], "u": ev.get("link") or ""})
    total = sum(cats.values())
    return {"total": total, "by_cat": cats, "items": items[:10]}


def parse_gdacs(text: str, day=None) -> dict:
    """GDACS RSS → 红/橙/绿预警计数（只算 day 当天发布的）。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    counts = {"red": 0, "orange": 0, "green": 0}
    items: list = []
    stamps = {it["t"]: it["d"] for it in parse_feed_items(text)} if day else {}
    for item in root.iter():
        if not item.tag.endswith("item"):
            continue
        if day:
            title = ""
            for ch in item:
                if ch.tag.split("}")[-1].lower() == "title":
                    title = (ch.text or "").strip()
            if not on_day(stamps.get(title), day):
                continue
        level = ""
        title = ""
        link = ""
        for child in item:
            name = child.tag.split("}")[-1].lower()
            text = (child.text or "").strip()
            if name in ("alertlevel", "alert_level", "gdacs_alertlevel"):
                level = text.lower()
            elif name == "title":
                title = text
            elif name == "link":
                link = text
        if level in counts:
            counts[level] += 1
            items.append({"t": title[:80], "v": level,
                          "ts": str(stamps.get(title) or "")[:16], "u": link})
    counts["total"] = sum(counts.values())
    order = {"red": 0, "orange": 1, "green": 2}
    items.sort(key=lambda x: order.get(x["v"], 9))
    counts["items"] = items[:12]
    return counts


def parse_kp(text: str, day=None) -> dict:
    """NOAA SWPC Kp 指数（两种格式都吃）。day 给定时只取当天。"""
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        return {}
    vals = []
    if doc and isinstance(doc[0], list):          # products 格式：[[header], [row], ...]
        for row in doc[1:]:
            if isinstance(row, list) and len(row) >= 2:
                if day and not str(row[0])[:10] == day.strftime("%Y-%m-%d"):
                    continue
                v = _f(row[1])
                if v is not None:
                    vals.append(v)
    elif doc and isinstance(doc[0], dict):        # json 格式：[{k_index: ...}, ...]
        for row in doc:
            if day:
                ts = str(row.get("time_tag") or "")[:10]
                if ts and ts != day.strftime("%Y-%m-%d"):
                    continue
            # 实测 products/noaa-planetary-k-index.json 的键是 "Kp"（大写），
            # 1m 文件用的是 "kp_index"——两个都得认，否则整段解析出空值。
            v = _f(row.get("Kp") or row.get("kp_index") or row.get("k_index") or row.get("kp"))
            if v is not None:
                vals.append(v)
    if not vals:
        return {}
    return {"max": max(vals), "mean": sum(vals) / len(vals), "n": len(vals)}


def fetch_day(day: date) -> tuple[dict, list[dict]]:
    """抓取某一天的灾害读数。返回 (读数, health)。

    只有地震能按日期回补（USGS 支持日期区间）；EONET/GDACS/SWPC 都是"当前快照"，
    因此历史只能靠每次运行往后累积——这也意味着灾害指数的历史在最初两周是空的，
    页面必须如实显示"累积中"，不能给个 50 蒙混过去。
    """
    out: dict[str, float] = {}
    health: list[dict] = []
    evidence: dict = {}
    s = day.strftime("%Y-%m-%d")
    e = (day + timedelta(days=1)).strftime("%Y-%m-%d")

    def rec(source: str, ok: bool, items: int, error: str = "") -> None:
        health.append({"source": source, "ok": ok, "items": items, "error": error[:200]})

    # 1) 地震
    try:
        data = _get_retry(USGS.format(mag=USGS_MIN_MAG, s=s, e=e), timeout=60)
        q = parse_usgs(data)
        if q:
            out["quake_energy"] = round(q["energy"], 3)
            out["quake_count"] = float(q["count"])
            out["quake_max"] = round(q["max"], 1)
            evidence["quake"] = q.get("items") or []
            rec("usgs:quake", True, q["count"])
        else:
            rec("usgs:quake", False, 0, "解析到 0 条")
    except Exception as exc:                                    # noqa: BLE001
        rec("usgs:quake", False, 0, f"{type(exc).__name__}: {exc}")

    # 2) NASA EONET
    try:
        data = _get_retry(EONET.format(days=2), timeout=60)
        ev = parse_eonet(data)
        if ev:
            out["storm_events"] = float(ev["total"])
            evidence["eonet"] = ev.get("items") or []
            rec("nasa:eonet", True, ev["total"])
        else:
            rec("nasa:eonet", False, 0, "解析到 0 条")
    except Exception as exc:                                    # noqa: BLE001
        rec("nasa:eonet", False, 0, f"{type(exc).__name__}: {exc}")

    # 3) GDACS
    try:
        data = _get_retry(GDACS, timeout=60)
        g = parse_gdacs(data, day=day)
        if g:
            out["gdacs_alert"] = float(g["red"] * 2 + g["orange"])
            evidence["gdacs"] = g.get("items") or []
            rec("gdacs:rss", True, g["total"])
        else:
            rec("gdacs:rss", False, 0, "解析到 0 条")
    except Exception as exc:                                    # noqa: BLE001
        rec("gdacs:rss", False, 0, f"{type(exc).__name__}: {exc}")

    # 4) 空间天气（Kp）
    kp_error = "全部候选地址失败"
    for url in SWPC:
        try:
            data = _get_retry(url, timeout=45)
            k = parse_kp(data, day=day)
            if k:
                out["space_weather"] = round(k["max"], 2)
                evidence["kp"] = [{"t": f"Kp {round(k['max'], 2)} 峰值（当日 {k['n']} 个时段）",
                                   "v": round(k["max"], 2), "ts": day.isoformat(),
                                   "u": "https://www.swpc.noaa.gov/products/planetary-k-index"}]
                rec("noaa:swpc", True, k["n"])
                break
        except Exception as exc:                                # noqa: BLE001
            kp_error = f"{type(exc).__name__}: {exc}"
    else:
        rec("noaa:swpc", False, 0, kp_error)

    return out, health, evidence
