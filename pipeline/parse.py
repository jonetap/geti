"""解析层：GDELT export CSV、RSS/Atom。全部标准库，无第三方依赖。"""
from __future__ import annotations

import csv
import html
import io
import re
import zipfile
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

from .model import EventSignal, RawItem, cameo_component, region_of

UTC = timezone.utc

# GDELT export 无表头、tab 分隔。两代格式列数不同，但前 35 列完全一致：
#   2.0（15 分钟文件）：61 列，ActionGeo_CountryCode=53，SOURCEURL=60
#   1.0（/events/ 日文件）：58 列，ActionGeo_CountryCode=51，SOURCEURL=57
# 差别在于 2.0 给 Actor1/Actor2/ActionGeo 各插了一个 ADM2 码。
# 只硬编码 61 会在 1.0 日文件上直接 IndexError —— 这是首次运行必踩的坑。
C_SQLDATE = 1
C_ACTOR1 = 6
C_ACTOR2 = 16
C_EVENTCODE = 26
C_GOLDSTEIN = 30
C_MENTIONS = 31
C_GEO_CC_20 = 53
C_GEO_CC_10 = 51
C_URL_20 = 60
C_URL_10 = 57

MAX_EVENTS_PER_FILE = 40000   # 单日文件约 13 万行，全留会把内存和 z 分数都拖垮

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(s: str) -> str:
    if not s:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", s))).strip()


# ---------------------------------------------------------------- GDELT


def _to_date(v: str, fallback: date | None = None) -> date | None:
    v = (v or "").strip()
    if len(v) >= 8 and v[:8].isdigit():
        try:
            return date(int(v[0:4]), int(v[4:6]), int(v[6:8]))
        except ValueError:
            return fallback
    return fallback


def gdelt_rows(text: str, keep_regions: set[str] | None = None,
               min_mentions: int = 2, min_severity: int = 2,
               max_events: int = MAX_EVENTS_PER_FILE) -> list[EventSignal]:
    """把 export.CSV 的文本切成 EventSignal，同时兼容 2.0（61 列）与 1.0（58 列）。

    两条过滤是必须的，不是可选优化：
      * `min_severity=2` 丢掉 01–08 的合作/表态类事件（占日文件一半以上），
        它们严重度只有 1，但数量足以把真实信号淹没在噪声里；
      * `min_mentions=2` 丢掉只被一家媒体提过一次的事件——那不是事件，是抓取噪声。
    """
    out: list[EventSignal] = []
    reader = csv.reader(io.StringIO(text), delimiter="\t", quoting=csv.QUOTE_NONE)
    for cols in reader:
        n = len(cols)
        if n < 58:
            continue
        if n >= 61:
            geo_idx, url_idx = C_GEO_CC_20, C_URL_20
        else:
            geo_idx, url_idx = C_GEO_CC_10, C_URL_10

        code = (cols[C_EVENTCODE] or "").strip()
        if not code.isdigit():
            continue
        comp, sev = cameo_component(code)
        if sev < min_severity:
            continue

        try:
            mentions = int(float(cols[C_MENTIONS] or 1))
        except ValueError:
            mentions = 1
        if mentions < min_mentions:
            continue

        day = _to_date(cols[C_SQLDATE])
        if day is None:
            continue
        fips = (cols[geo_idx] or "").strip()
        reg = region_of(fips)
        if keep_regions is not None and reg not in keep_regions:
            continue
        try:
            gold = float(cols[C_GOLDSTEIN] or 0.0)
        except ValueError:
            gold = 0.0

        a1, a2 = (cols[C_ACTOR1] or "").strip(), (cols[C_ACTOR2] or "").strip()
        out.append(EventSignal(
            day=day,
            fips=fips,
            region=reg,
            cameo=code,
            component=comp,
            severity=sev,
            goldstein=gold,
            mentions=max(1, mentions),
            eid=(cols[0] or "").strip(),
            actors=f"{a1} → {a2}".strip(" →"),
            url=(cols[url_idx] or "").strip(),
        ))
        if len(out) >= max_events:
            break
    return out


def gdelt_from_zip(data: bytes, keep_regions: set[str] | None = None,
                   **kwargs) -> list[EventSignal]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        name = next((n for n in zf.namelist() if n.lower().endswith(".csv")), None)
        if name is None:
            return []
        raw = zf.read(name)
    return gdelt_rows(raw.decode("utf-8", errors="replace"), keep_regions, **kwargs)


# ---------------------------------------------------------------- RSS / Atom


def _text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def _local(tag: str) -> str:
    """去掉 {namespace} 前缀。RSS 1.0（RDF）把 item 放在默认命名空间里，
    用 iter("item") 一条都匹配不到——这就是 DW 那种源读出 0 条的原因。"""
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if isinstance(c.tag, str) and _local(c.tag) == name:
            return c
    return None


def _atom_link(entry: ET.Element, ns: str) -> str:
    for link in entry.findall(f"{ns}link"):
        href = link.get("href")
        if href and link.get("rel", "alternate") == "alternate":
            return href
    return ""


def _parse_time(v: str, default: "datetime | None" = None) -> "datetime | None":
    """解析发布时间。**解析不出来就返回 None，绝不用"现在"顶上。**

    这是一次事故换来的：新华网的国际 RSS 是一个从 2022-12 就不再更新的死档案，
    而且条目里根本没有 pubDate。原先这里拿 now 当默认值，于是 2022 年的新闻
    全部变成"刚刚发生"，还进了"突发"。空值不是"现在"，是"不知道"。
    """
    v = (v or "").strip()
    if not v:
        return default
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except (TypeError, ValueError):
        pass
    try:
        s = v.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return default


def parse_feed(xml_text: str, source_id: str, now: datetime | None = None,
               lang: str = "en") -> list[RawItem]:
    """同时兼容 RSS 2.0 与 Atom。坏条目单条跳过，不整批失败。"""
    now = now or datetime.now(UTC)
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return []

    items: list[RawItem] = []

    # RSS 2.0 / RSS 1.0（RDF）：按本地名匹配，两种格式都能读
    for it in root.iter():
        if not isinstance(it.tag, str) or _local(it.tag) != "item":
            continue
        title = strip_html(_text(_child(it, "title")))
        if not title:
            continue
        body = strip_html(_text(_child(it, "description")))
        link = _text(_child(it, "link"))
        if not link:
            guid = _child(it, "guid")
            link = _text(guid) if guid is not None and _text(guid).startswith("http") else ""
        raw_time = _text(_child(it, "pubDate")) or _text(_child(it, "date")) \
            or _text(_child(it, "published"))
        pub = _parse_time(raw_time)
        items.append(RawItem(source_id, title, body, link, pub, lang))

    # Atom（有些源用 entries，xmlns 命名空间必须显式带）
    if not items:
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"
        for entry in root.iter(f"{ns}entry"):
            title = strip_html(_text(entry.find(f"{ns}title")))
            if not title:
                continue
            body = strip_html(_text(entry.find(f"{ns}summary")) or _text(entry.find(f"{ns}content")))
            link = _atom_link(entry, ns)
            pub = _parse_time(_text(entry.find(f"{ns}updated")) or _text(entry.find(f"{ns}published")))
            items.append(RawItem(source_id, title, body, link, pub, lang))

    return items


# ---------------------------------------------------------------- 官方页

_ANCHOR_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>([^<]{8,200})</a>', re.I)


def parse_official_listing(page_html: str, source_id: str, now: datetime | None = None,
                           base: str = "", lang: str = "zh") -> list[RawItem]:
    """从没有 RSS 的官方站点列表页里粗提标题+链接。

    这是"能跑就行"的启发式：官方页改版会让它失效，所以必须配 adapter 健康监控。
    """
    now = now or datetime.now(UTC)
    out: list[RawItem] = []
    for href, text in _ANCHOR_RE.findall(page_html or ""):
        title = strip_html(text)
        if len(title) < 8:
            continue
        url = href if href.startswith("http") else (base.rstrip("/") + "/" + href.lstrip("/"))
        out.append(RawItem(source_id, title, "", url, now, lang))
    return out
