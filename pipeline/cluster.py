"""去重与聚类：SimHash + 标题归一化。

为什么不用 LLM 去重：3,000 条两两比对是 O(n²)，用 SimHash 是纯 CPU + 零 token。
聚类结果按"独立起源"计——十家转载同一篇通稿只算 1 个源（DESIGN.md §5.1②）。
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Iterable

from .model import Cluster, RawItem

_PUNCT_RE = re.compile(
    r"[\s\u3000!-/:-@\[-`{-~，。！？、；：“”‘’（）《》〈〉【】…—·]+"
)


def normalize(text: str) -> str:
    return _PUNCT_RE.sub("", (text or "").lower())


def shingles(text: str, k: int = 3) -> set[str]:
    s = normalize(text)
    if not s:
        return set()
    if len(s) <= k:
        return {s}
    return {s[i:i + k] for i in range(len(s) - k + 1)}


def simhash(text: str, k: int = 3) -> int:
    """64 位 SimHash。空文本返回 0。"""
    grams = shingles(text, k)
    if not grams:
        return 0
    acc = [0] * 64
    for g in grams:
        h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:8], "big")
        for i in range(64):
            acc[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(64):
        if acc[i] > 0:
            out |= 1 << i
    return out


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def jaccard(a: str, b: str, k: int = 2) -> float:
    """字符 k-gram 的 Jaccard 相似度。中英通吃，不需要分词。"""
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    return inter / (len(sa) + len(sb) - inter)


def containment(a: str, b: str, k: int = 2) -> float:
    """包含度 = 交集 / 较短者。

    媒体转载时最爱加后缀（"（更新）"、"，多方证实"、"——现场画面流出"），
    Jaccard 会被这些后缀拉低到阈值以下，包含度不会。
    """
    sa, sb = shingles(a, k), shingles(b, k)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def similar(a: str, b: str) -> bool:
    return jaccard(a, b) >= 0.72 or containment(a, b) >= 0.82


# ---------------------------------------------------------------- 特异性

_DATE_RE = re.compile(r"(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}/\d{1,2}|\d{1,2}:\d{2})")
_QTY_RE = re.compile(r"\d+(?:\.\d+)?\s*(辆|人|名|架|枚|艘|吨|公里|千米|km|亿|万|千|美元|元|%|％)")
_UNIT_RE = re.compile(r"(第[\w\u4e00-\u9fa5]{1,8}(?:旅|师|军|集团军|舰队|营|团))|([A-Z][a-z]+(?: Brigade| Division| Fleet| Battalion))", re.I)
_PLACE_RE = re.compile(r"(省|市|州|港|机场|口岸|海峡|半岛|边境|海域|空域|基地|\b(?:Strait|Port|Airport|Base|Border|Sea)\b)")
_URL_ANCHOR_RE = re.compile(r"(NOTAM|航行通告|卫星图|SAT|ADS-B|航班号|NOTAMs?)", re.I)


def specificity(text: str) -> float:
    """0-1：这条信息含多少"可核对的具体性"。

    模糊的信息更容易是编的；具体到时间/地点/数量/单位的，即便来源差也更值钱。
    """
    t = text or ""
    hits = 0
    hits += 1 if _DATE_RE.search(t) else 0
    hits += 1 if _QTY_RE.search(t) else 0
    hits += 1 if _UNIT_RE.search(t) else 0
    hits += 1 if _PLACE_RE.search(t) else 0
    hits += 1 if _URL_ANCHOR_RE.search(t) else 0
    return min(1.0, hits / 4.0)


# ---------------------------------------------------------------- 聚类


def cluster_items(items: Iterable[RawItem], threshold: int = 6,
                  max_gap_hours: float = 36.0) -> list[Cluster]:
    """贪心聚类：SimHash 距离 <= threshold，或标题足够相似，且时间相邻。

    时间窗口是必须的：同一句"边境交火"可能是周一和周三两次不同的事件，
    按文本合并会把它们算成"一个事件被反复报道"，直接抹掉一次升级。
    """
    ordered = sorted((it for it in items if it.published is not None),
                     key=lambda x: x.published, reverse=True)
    clusters: list[Cluster] = []
    reps: list[int] = []
    seen_url: dict[str, Cluster] = {}

    for it in ordered:
        if it.url:
            hit = seen_url.get(it.url)
            if hit is not None:
                hit.items.append(it)
                continue

        h = simhash(it.title)
        best = -1
        for i, rh in enumerate(reps):
            gap = abs((clusters[i].published - it.published).total_seconds()) / 3600.0
            if gap > max_gap_hours:
                continue
            if hamming(h, rh) <= threshold or similar(it.title, clusters[i].title):
                best = i
                break

        if best >= 0:
            clusters[best].items.append(it)
            if it.url:
                seen_url[it.url] = clusters[best]
            continue

        cid = hashlib.sha1(f"{it.source_id}|{it.url or it.title}".encode()).hexdigest()[:12]
        c = Cluster(cid=cid, title=it.title, body=it.body, url=it.url,
                    published=it.published, items=[it])
        clusters.append(c)
        reps.append(h)
        if it.url:
            seen_url[it.url] = c

    return clusters


def merge_by_origin(clusters: list[Cluster]) -> list[Cluster]:
    """把"同一事件、但标题差异过大"的簇再按正文重合度合并一次。

    比标题更保守（阈值 5），避免把不同事件误并。
    """
    merged: list[Cluster] = []
    reps: list[int] = []
    for c in sorted(clusters, key=lambda x: x.published, reverse=True):
        h = simhash(f"{c.title} {c.body[:120]}")
        done = False
        for i, rh in enumerate(reps):
            if hamming(h, rh) <= 5:
                merged[i].items.extend(c.items)
                if len(c.body) > len(merged[i].body):
                    merged[i].body = c.body
                done = True
                break
        if not done:
            merged.append(c)
            reps.append(h)
    return merged
