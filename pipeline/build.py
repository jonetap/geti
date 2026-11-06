"""把信号与新闻簇组装成 snapshot.json。

这一层不做 IO，只做纯计算，方便测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Sequence

from . import score
from .cluster import specificity
from .model import (
    COMPONENT_LABELS,
    COMPONENT_WEIGHTS,
    REGION_LABELS,
    Cluster,
    EventSignal,
    ScoredCluster,
    source as get_source,
)

UTC = timezone.utc
INDEX_VERSION = "geti-0.4.3"
HISTORY_DAYS = 90
BREAKING_HOURS = 8.0          # 超过 8 小时的旧事，不叫"突发"
MIN_ITEMS_PER_DAY = 20


# ---------------------------------------------------------------- 地理推断

_REGION_WORDS: dict[str, tuple[str, ...]] = {
    "EAST_ASIA": ("中国", "北京", "台海", "台湾", "日本", "韩国", "朝鲜", "东海", "南海", "钓鱼岛",
                  "china", "beijing", "taiwan", "japan", "korea", "pyongyang", "tokyo"),
    "SOUTH_ASIA": ("印度", "巴基斯坦", "克什米尔", "孟加拉", "阿富汗", "斯里兰卡",
                   "india", "pakistan", "kashmir", "bangladesh", "afghan", "delhi"),
    "MIDDLE_EAST": ("以色列", "伊朗", "加沙", "黎巴嫩", "叙利亚", "也门", "胡塞", "沙特", "土耳其",
                    "伊拉克", "德黑兰", "红海", "霍尔木兹",
                    "israel", "iran", "gaza", "lebanon", "syria", "yemen", "houthi", "saudi",
                    "turkey", "iraq", "tehran", "red sea", "hormuz"),
    "EUROPE": ("欧洲", "欧盟", "北约", "德国", "法国", "英国", "波兰", "波罗的海", "巴尔干",
               "europe", "eu ", "nato", "germany", "france", "britain", "poland", "baltic"),
    "RUSSIA_CIS": ("俄罗斯", "乌克兰", "莫斯科", "基辅", "白俄罗斯", "克里米亚", "顿巴斯", "高加索",
                   "russia", "ukraine", "moscow", "kyiv", "kiev", "belarus", "crimea", "donbas",
                   "caucasus", "kazakh"),
    "AFRICA": ("非洲", "苏丹", "埃塞俄比亚", "尼日利亚", "索马里", "利比亚", "刚果", "萨赫勒",
               "africa", "sudan", "ethiopia", "nigeria", "somalia", "libya", "congo", "sahel"),
    "NORTH_AMERICA": ("美国", "加拿大", "华盛顿", "五角大楼",
                      "united states", "u.s.", "washington", "pentagon", "canada"),
    "LATAM": ("拉美", "委内瑞拉", "巴西", "墨西哥", "哥伦比亚", "阿根廷", "海地",
              "venezuela", "brazil", "mexico", "colombia", "argentina", "haiti"),
    "OCEANIA": ("澳大利亚", "新西兰", "太平洋岛国", "australia", "new zealand", "pacific island"),
}


def infer_region(text: str) -> str:
    low = (text or "").lower()
    best, best_n = "OTHER", 0
    for reg, words in _REGION_WORDS.items():
        n = sum(1 for w in words if w.lower() in low)
        if n > best_n:
            best, best_n = reg, n
    return best


# ---------------------------------------------------------------- 压力序列

Series = dict[tuple[str, str], dict[date, float]]

# 每天处理的原始条目数，记为一条"虚拟序列"，用来做体量归一化。
# GDELT 日文件 1.6 万到 2.6 万条是常态，涨 60% 不等于局势变紧 60%——
# 那只是那天新闻多。不归一化，指数就会跟着抓取量起伏，而不是跟着局势。
VOLUME_KEY = ("__vol__", "n")


def pressure_from_signals(signals: Iterable[EventSignal]) -> Series:
    """GDELT 这类结构化的、不需要 LLM 的信号。"""
    s: Series = {}
    # 信息/叙事：不用"事件数量"，用"平均报道放大倍数"。
    # 事件多 ≠ 叙事强——同一件事被更多家媒体反复咬住，才是舆情在升温。
    # 逐条累加 (严重度加权 × log(提及数))，最后除以条数，得到当天的平均 salience。
    # GDELT 2.0 的 15 分钟文件里，同一事件会随报道增加反复出现（NumMentions 递增）。
    # 不去重的话，一场空袭在 36 小时里会被记几十遍，指数直接顶死。
    if any(getattr(ev, "eid", "") for ev in signals):
        best: dict = {}
        rest: list = []
        for ev in signals:
            eid = getattr(ev, "eid", "")
            if not eid:
                rest.append(ev)
                continue
            cur = best.get(eid)
            if cur is None or ev.mentions > cur.mentions:
                best[eid] = ev
        signals = list(best.values()) + rest

    narr: dict = {}
    for ev in signals:
        base = ev.severity / 5.0
        salience = 1.0 + 0.15 * math.log1p(ev.mentions)
        if ev.severity >= 3:
            slot = narr.setdefault((ev.region, ev.day), [0.0, 0])
            slot[0] += base * math.log1p(ev.mentions)
            slot[1] += 1
        vol = s.setdefault(VOLUME_KEY, {})
        vol[ev.day] = vol.get(ev.day, 0.0) + 1.0
        if ev.goldstein > 1.5:
            w = 0.5 * base * salience           # 合作事件：记为去紧张
            cell = s.setdefault((ev.region, ev.component), {})
            cell[ev.day] = cell.get(ev.day, 0.0) - w
        else:
            w = base * salience * (1.0 if ev.goldstein <= 0 else 0.7)
            cell = s.setdefault((ev.region, ev.component), {})
            cell[ev.day] = cell.get(ev.day, 0.0) + w
    # 平均报道放大倍数：把「事件多」这个因素除掉，只留「被放大了多少」
    for (region, day), (tot, n) in narr.items():
        if n < 3:
            continue
        cell = s.setdefault((region, "narrative"), {})
        cell[day] = tot / n
    return s


def drop_failed_days(series: Series) -> tuple[Series, list[str]]:
    """丢掉几乎什么都没抓到的日子。

    判据只看绝对量（< 20 条即视为没抓到），**不看中位数**——中位数本身会被
    大批残缺日子拉低，用它当基准等于拿病历当健康标准（我们试过，只剔掉 3 天）。
    """
    vol = series.get(VOLUME_KEY, {})
    if not vol:
        return series, []
    bad = {d for d, n in vol.items() if n < MIN_ITEMS_PER_DAY}
    if not bad:
        return series, []
    out: Series = {}
    for k, days in series.items():
        out[k] = {d: v for d, v in days.items() if d not in bad}
    return out, sorted(d.isoformat() for d in bad)


def share_normalize(series: Series) -> Series:
    """把"当日压力绝对值"折成"当日份额 × log(当日总量)"。

    为什么必须折：1.0 的历史日文件时好时坏，某些日子只解析出几十条、某些日子
    几万条，**相邻两天差 300 倍**。用绝对量做 z 分数，今天永远是最极端的那个，
    和"世界真的更紧张"在数值上无法区分。

    除以当日总量，抓取规模就被消掉了，剩下的是"这一天里军事升级占多大比重"——
    这才是跨日可比的。再乘 `log1p(总量)` 保留一点"真的更热闹"的权重：对数意味着
    300 倍的抓取误差最多只放大 1.4 倍，而总量的真实变化仍然进得来。
    """
    totals: dict[date, float] = {}
    for k, cell in series.items():
        if k == VOLUME_KEY or k[1] == "physical":
            continue      # 空域份额不是"证据累加"，混进来会把物理分项压成 0
        for d, v in cell.items():
            if v > 0:
                totals[d] = totals.get(d, 0.0) + v
    if not totals:
        return series
    out: Series = {}
    for k, cell in series.items():
        if k == VOLUME_KEY:
            continue
        if k[1] == "physical":
            out[k] = dict(cell)        # 原样保留，量纲交给 robust_z
            continue
        norm: dict[date, float] = {}
        for d, v in cell.items():
            tot = totals.get(d, 0.0)
            if v <= 0 or tot <= 0:
                norm[d] = 0.0
                continue
            norm[d] = math.sqrt(math.log1p(v) * (v / tot) * math.log1p(tot))
        out[k] = norm
    return out


def pressure_from_clusters(scored: Iterable[ScoredCluster]) -> Series:
    """新闻簇：证据量已经合成了可信度与特异性。"""
    s: Series = {}
    for sc in scored:
        if sc.component == "narrative":
            continue  # narrative 已由 GDELT 权重给出，别两种量纲相加
        day = sc.cluster.published.date()
        reg = infer_region(f"{sc.cluster.title} {sc.cluster.body[:200]}")
        cell = s.setdefault((reg, sc.component), {})
        cell[day] = cell.get(day, 0.0) + sc.evidence
    return s


def merge_series(*parts: Series) -> Series:
    out: Series = {}
    for p in parts:
        for k, days in p.items():
            cell = out.setdefault(k, {})
            for d, v in days.items():
                cell[d] = cell.get(d, 0.0) + v
    return out


# ---------------------------------------------------------------- 指数


def _trailing(series: Series, key: tuple[str, str], day: date, n: int) -> list[float]:
    """取 day 之前 n 天的历史。

    关键：**观测开始之前的日期不算 0**。否则新纳入监控的区域，第一天就会
    因为"历史全是 0、今天有动静"而被判成极端异常——这是最典型的新源假警报。
    观测开始之后、当天无事件的日期才记 0（沉默本身是信息）。
    """
    cell = series.get(key, {})
    if not cell:
        return []
    first = min(cell)
    out: list[float] = []
    for i in range(n, 0, -1):
        d = day - timedelta(days=i)
        if d < first:
            continue
        out.append(cell.get(d, 0.0))
    return out


def _value(series: Series, key: tuple[str, str], day: date) -> float:
    return series.get(key, {}).get(day, 0.0)


@dataclass
class DayResult:
    day: date
    regions: dict[str, float]
    zvec: dict[str, dict[str, float]]     # region -> comp -> z
    covered: int                          # 有 z 的 (区域×分项) 个数
    required: int
    saturated: int = 0                    # 贴到软压缩上限的 (区域×分项) 个数

    @property
    def aggregate_regions(self) -> list[float]:
        """参与全球指数的区域：排除"其他"（地理无法归属的事件）。

        未归属的压力仍然展示在区域面板里，但不许它主导头条指数——
        否则一批没说清在哪的新闻能把全球读数直接顶满。
        """
        return [v for r, v in self.regions.items() if r != "OTHER"]


MIN_COMPONENTS = 2   # 少于 2 个分项有 z 就不给这个区域打分（证据太薄）


def compute_day(series: Series, day: date, regions: Sequence[str]) -> DayResult:
    per_region: dict[str, float] = {}
    zmap: dict[str, dict[str, float]] = {}
    ok = 0
    total = 0
    saturated = 0
    for r in regions:
        zvec: dict[str, float] = {}
        for c in COMPONENT_WEIGHTS:
            total += 1
            hist = _trailing(series, (r, c), day, HISTORY_DAYS)
            # 对数尺度：压力是乘性的，线性 z 会把重尾算成"天天爆表"
            z = score.robust_z(
                score.signed_log(_value(series, (r, c), day)),
                [score.signed_log(v) for v in hist],
            )
            if z is None:
                continue
            ok += 1
            if score.is_saturated(z):
                saturated += 1
            if abs(z) > 0.01:
                zvec[c] = z
        if len(zvec) >= MIN_COMPONENTS:
            # 不做权重归一化：缺的分项就是缺证据，不该被放大成满分
            per_region[r] = score.combine(zvec)
            zmap[r] = zvec
    return DayResult(day=day, regions=per_region, zvec=zmap, covered=ok,
                     required=max(1, total), saturated=saturated)


def compute_window(series: Series, days: Sequence[date], regions: Sequence[str]) -> dict[date, DayResult]:
    return {d: compute_day(series, d, regions) for d in days}


AGG_SCALE = 1.2           # 聚合层 z → 0-100 的缩放
AGG_ALPHA = 0.35          # 聚合层 EWMA 平滑系数
MIN_AGG_HISTORY = 14


def index_series(results: dict[date, DayResult], days: Sequence[date]) -> tuple[list[float], float | None]:
    """两级标准化 + 聚合层平滑。

    第一级在每个区域内按分项做稳健 z（score.combine）。
    第二级对聚合值（0.5·max + 0.5·P90）再按它自己的历史做一次稳健 z。

    第二级不能省：max 是次序统计量，取 6 个区域的最大值天然偏高，
    若直接映射到 0-100，一个"普通的一天"也会读出 70 分，仪表盘天天报红，
    三个月后没人再信它。做完第二级，50 分才真正等于"和过去一样"。

    平滑也不能省：新闻量是泊松噪声，逐日原始值会让指数天天跳 40 分，
    那是噪声不是局势。EWMA(0.35) 大约把响应压到 1–2 天，足够快也足够稳。
    """
    raw: list[float | None] = []
    for d in days:
        r = results.get(d)
        raw.append(None if (r is None or not r.aggregate_regions) else score.global_index(r.aggregate_regions))

    # 先对"原始聚合值"做 z，再平滑指数。
    # 顺序反了会骗人：先平滑再算 z，等于拿平滑后的分布（MAD 更小）当分母，
    # z 被系统放大约 1.6 倍，指数会在无关紧要的日子冲到 90。
    zs: list[float | None] = []
    for i, a in enumerate(raw):
        if a is None:
            zs.append(None)
            continue
        hist = [x for x in raw[max(0, i - HISTORY_DAYS):i] if x is not None]
        zs.append(score.robust_z(a, hist) if len(hist) >= MIN_AGG_HISTORY else None)

    out: list[float] = []
    today_z: float | None = None
    prev: float | None = None
    for i, z in enumerate(zs):
        if i == len(zs) - 1:
            today_z = z
        s = 50.0 if z is None else 50.0 + 50.0 * math.tanh(z / AGG_SCALE)
        prev = s if prev is None else AGG_ALPHA * s + (1.0 - AGG_ALPHA) * prev
        out.append(prev)
    return out, today_z


# ---------------------------------------------------------------- 快照


def _diagnostics(series: Series, day: date, driver: str, zvec: dict[str, float]) -> dict:
    """驱动区域每个分项的原始压力 vs 历史分位。

    线上只剩一个 z 的时候，没法区分"今天真的大涨"和"尺度算坏了"——第一版
    四个分项同时顶在 3.0 就是这个盲点。把 now / p50 / p90 / 倍数一起吐出来，
    下次出事肉眼就能定位到是数据的问题还是标定的问题。
    """
    rows: dict[str, dict] = {}
    for c in COMPONENT_WEIGHTS:
        hist = _trailing(series, (driver, c), day, HISTORY_DAYS)
        now_v = _value(series, (driver, c), day)
        med = score.median(hist) if hist else None
        rows[c] = {
            "now": round(now_v, 2),
            "p50": None if med is None else round(med, 2),
            "p90": None if not hist else round(score.p90(hist), 2),
            "ratio": None if not med or abs(med) < 0.1 else round(now_v / med, 2),
            "z": None if c not in zvec else round(zvec[c], 2),
        }
    return {"region": driver, "history_days": HISTORY_DAYS, "components": rows}


def build_snapshot(
    series: Series,
    target: date,
    days: Sequence[date],
    scored: Sequence[ScoredCluster],
    coverage: dict,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(UTC)
    series, failed_days = drop_failed_days(series)
    series = share_normalize(series)
    coverage = dict(coverage or {})
    if failed_days:
        coverage["failed_days"] = len(failed_days)
        coverage["failed_days_last"] = failed_days[-5:]
    regions = sorted({r for (r, _c) in series if not str(r).startswith("__")})
    for r in REGION_LABELS:
        if r not in regions:
            regions.append(r)

    results = compute_window(series, days, regions)
    if target not in results:
        raise ValueError(f"target {target} 不在窗口内")

    today = results[target]
    gseries, dz = index_series(results, days)
    geti = gseries[-1] if gseries else 50.0
    prev = gseries[-2] if len(gseries) > 1 else geti

    ranked = [r for r in today.regions if r != "OTHER"]
    driver = max(ranked, key=lambda r: today.regions[r]) if ranked else "OTHER"
    zvec = today.zvec.get(driver, {})
    numer = {c: COMPONENT_WEIGHTS.get(c, 0.0) * z for c, z in zvec.items()}
    denom = sum(abs(v) for v in numer.values()) or 1.0

    components = []
    avail = [c for c in COMPONENT_LABELS if zvec.get(c) is not None]
    avail_w = sum(COMPONENT_WEIGHTS[c] for c in avail) or 1.0
    for c, label in COMPONENT_LABELS.items():
        z = zvec.get(c)
        components.append({
            "key": c,
            "label": label,
            "weight": COMPONENT_WEIGHTS[c],
            # 「设计权重」和「这次实际生效的权重」必须分开显示。物理征兆和信息叙事
            # 目前没有历史（证据只来自新闻簇），它们缺席时如果还按原权重展示，
            # 页面会假装六个分项都在参与计算。宁可写清楚"这次只有 4/6 有效"。
            "effective_weight": 0.0 if z is None else round(COMPONENT_WEIGHTS[c] / avail_w, 3),
            "available": z is not None,
            "z": None if z is None else round(z, 2),
            "score": None if z is None else round(score.to_index(z), 1),
            "contrib": round(numer.get(c, 0.0) / denom, 3),
        })

    region_rows = []
    for r, v in sorted(today.regions.items(), key=lambda kv: -kv[1]):
        prev_r = 0.0
        if len(days) > 1:
            prev_r = results[days[-2]].regions.get(r, v)
        region_rows.append({
            "code": r,
            "label": REGION_LABELS.get(r, r),
            "score": round(v, 1),
            "delta": round(v - prev_r, 1),
            "excluded": r == "OTHER",
            "drivers": sorted(
                ((c, round(zz, 2)) for c, zz in today.zvec.get(r, {}).items()),
                key=lambda kv: -abs(kv[1]),
            )[:2],
        })

    events = sorted(
        scored,
        key=lambda s: (s.severity, s.confidence, s.cluster.published),
        reverse=True,
    )[:24]

    def _event_row(sc: ScoredCluster) -> dict:
        c = sc.cluster
        return {
            "id": c.cid,
            "title": c.title,
            "url": c.url,
            "component": sc.component,
            "severity": sc.severity,
            "confidence": round(sc.confidence, 2),
            "origins": sc.origin_count,
            "sources": [it.source_id for it in c.items][:6],
            "published": c.published.isoformat() if c.published else "",
            "note": c.body[:180],
            "region": infer_region(f"{c.title} {c.body[:120]}"),
        }

    event_rows = [_event_row(s) for s in events]

    # "突发"= 又严重、又**确实刚发生**。原先的条件是 severity>=4 且
    # （单源 或 3 小时内），于是"单源 + 四年前"也满足——这是 2022 年旧闻
    # 混进突发的入口。现在新鲜度是硬门槛，单源只用来决定排序（待核实优先）。
    def _age_h(published: str) -> float:
        try:
            t = datetime.fromisoformat(published or "")
        except ValueError:
            return 999.0
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return max(0.0, (now - t).total_seconds() / 3600.0)

    _recent = [e for e in event_rows
               if e["severity"] >= 4 and _age_h(e["published"]) <= BREAKING_HOURS]
    _recent.sort(key=lambda e: (_age_h(e["published"]), e["origins"]))
    breaking = _recent[:4]

    conf = 0.0
    conf += 0.5 * min(1.0, coverage.get("items", 0) / 300.0)
    conf += 0.3 * min(1.0, coverage.get("sources_ok", 0) / 8.0)
    conf += 0.2 * min(1.0, today.covered / today.required)

    return {
        "index_version": INDEX_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "date": target.isoformat(),
        "geti": round(geti, 1),
        "delta_z": None if dz is None else round(dz, 2),
        "delta_pts": round(geti - prev, 1),
        "confidence": round(min(1.0, conf), 2),
        "history_days": len(days),
        "driver_region": driver,
        "driver_label": REGION_LABELS.get(driver, driver),
        "regions": region_rows,
        "components": components,
        "events": event_rows,
        "breaking": breaking,
        "coverage": {
            **coverage,
            "components_covered": today.covered,
            "components_required": today.required,
            "components_saturated": today.saturated,
            "saturated_ratio": round(today.saturated / max(1, today.covered), 3),
            "note": "覆盖不足时请看置信度，不要看指数",
        },
        # 标定诊断：驱动区域每个分项的"今天 vs 历史中位数/P90"。
        # 没有这几行，线上只剩一个 z，看不出到底是真涨还是尺度坏了。
        "diagnostics": _diagnostics(series, target, driver, today.zvec.get(driver, {})),
        "series": [
            {"date": d.isoformat(), "geti": round(v, 1)}
            for d, v in zip(days, gseries)
        ],
    }


# ---------------------------------------------------------------- 新闻簇打分


def score_clusters(clusters: Sequence[Cluster], now: datetime | None = None) -> list[ScoredCluster]:
    """把聚类结果转成带可信度/严重度/证据量的记录。

    注意：这里用的是**独立起源**，不是条目数。三家转载同一通稿只算一个起源。
    """
    now = now or datetime.now(UTC)
    out: list[ScoredCluster] = []
    for c in clusters:
        origins: list[tuple[float, float]] = []
        seen: set[str] = set()
        for it in c.items:
            if it.source_id in seen:
                continue
            seen.add(it.source_id)
            age_h = max(0.0, (now - it.published).total_seconds() / 3600.0)
            origins.append((get_source(it.source_id).prior, score.recency_decay(age_h)))
        conf = score.confidence(origins)
        text = f"{c.title} {c.body}"
        sev = score.severity_from_text(c.title, c.body)
        spec = specificity(text)
        comp = score.component_from_text(c.title, c.body)
        out.append(ScoredCluster(
            cluster=c,
            component=comp,
            severity=sev,
            confidence=conf,
            evidence=score.evidence(sev, conf, spec),
            corroboration=min(c.n_origin, 5) / 5.0,
            specificity=spec,
            origin_count=c.n_origin,
        ))
    return out
