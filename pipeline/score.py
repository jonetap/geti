"""打分核心：可信度合成、证据量、分项/区域/全球指数。

公式对齐 DESIGN.md §4 / §5.3：
    confidence = (1 − Π(1 − cᵢ·dᵢ)) · (0.70 + 0.30·min(n,3)/3)
    evidence   = (severity/5) · √confidence · (0.85 + 0.30·specificity)
    S          = 50 + 50·tanh( Σ w·z̃ / 1.6 )          ← 中性 = 50
    GETI       = 0.5·max(S_region) + 0.5·P90(S_region)
    Δ          = (S_t − EWMA_{t−1}) / (1.4826·MAD)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .model import COMPONENT_WEIGHTS

MIN_HISTORY_DAYS = 14       # 低于这个天数不给 z，只给"覆盖不足"
EWMA_LAMBDA = 0.25
Z_CLIP = 3.0
TANH_SCALE = 1.6
DECAY_6H = 0.6
DECAY_24H = 0.3


# ---------------------------------------------------------------- 稳健统计


def median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def mad(xs: Sequence[float], med: float | None = None) -> float:
    if not xs:
        return 0.0
    m = median(xs) if med is None else med
    return median([abs(x - m) for x in xs])


def stdev(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def robust_z(x: float, hist: Sequence[float]) -> float | None:
    """中位数 + MAD 标准化。历史不足返回 None（宁可不给数，也不给假数）。

    边界处理：某个区域连续 90 天毫无动静、某天突然出事时 MAD=0，
    这种情况必须给最大惊讶度，而不是 0（把它当"正常"是最危险的 bug）。
    """
    if len(hist) < MIN_HISTORY_DAYS:
        return None
    med = median(hist)
    scale = 1.4826 * mad(hist, med)
    if scale < 1e-9:
        scale = stdev(hist)
    if scale < 1e-9:
        if abs(x - med) < 1e-9:
            return None
        return Z_CLIP if x > med else -Z_CLIP
    return max(-Z_CLIP, min(Z_CLIP, (x - med) / scale))


def p90(xs: Sequence[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(0.9 * (len(s) - 1)))))
    return s[i]


def to_index(zsum: float) -> float:
    """Σw·z̃ -> 0-100，中性 50。"""
    return 50.0 + 50.0 * math.tanh(zsum / TANH_SCALE)


# ---------------------------------------------------------------- 可信度


def confidence(origins: Iterable[tuple[float, float]]) -> float:
    """origins: [(credibility, time_decay)]，每个独立起源一项。

    单源 0.9 -> 0.90；3 个 0.8 的独立源 -> 0.99；5 个 0.2 的小道源 -> 0.67。
    这就是"五个低权重源汇聚"能压过"一个中等源"的机制。
    """
    lst = list(origins)
    if not lst:
        return 0.0
    miss = 1.0
    for cred, decay in lst:
        miss *= (1.0 - max(0.0, min(1.0, cred * decay)))
    base = 1.0 - miss
    diversity = 0.70 + 0.30 * min(len(lst), 3) / 3.0
    return base * diversity


def recency_decay(age_hours: float) -> float:
    if age_hours <= 6:
        return 1.0
    if age_hours <= 24:
        return DECAY_6H
    return DECAY_24H


def evidence(severity: int, conf: float, spec: float) -> float:
    """计入压力的证据量。严重度线性、可信度开方（凹函数）。"""
    sev = max(1, min(5, int(severity)))
    return (sev / 5.0) * math.sqrt(max(0.0, conf)) * (0.85 + 0.30 * max(0.0, min(1.0, spec)))


# ---------------------------------------------------------------- 无 LLM 兜底


_SEV_PATTERNS: list[tuple[int, re.Pattern[str]]] = [
    (5, re.compile(r"(开战|宣战|入侵|空袭|导弹袭击|核打击|地面进攻|交火|击落|爆炸|炮击|"
                   r"declare war|invasion|airstrike|missile strike|nuclear strike|offensive|shelling|"
                   r"shot down|explosion|combat)", re.I)),
    (4, re.compile(r"(动员|集结|增兵|撤离|撤侨|断交|制裁|封锁|击沉|袭击|召回大使|"
                   r"暂停外交关系|暂停大使级|"
                   r"mobiliz|deploy|evacuat|sever relations|sanction|blockade|assault|strike)", re.I)),
    (3, re.compile(r"(军演|演习|联合演习|召见|驱逐|谈判破裂|抗议|示威|戒严|"
                   r"出口管制|禁运|关税|断供|配额|"
                   r"drill|exercise|summon|expel|talks collapse|protest|martial law|"
                   r"export control|embargo|tariff)", re.I)),
    (2, re.compile(r"(谴责|警告|批评|反对|威胁|未达成|会谈未果|无果而终|"
                   r"condemn|warn|criticiz|oppose|threaten|no agreement|inconclusive)", re.I)),
]

_COMP_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 注意：不要用裸"核"——"核实"会命中；不要用裸"军"——"冠军"会命中。
    ("military", re.compile(r"(军方|军队|军事|军演|军售|军备|部队|导弹|战机|舰船|军舰|"
                            r"演习|空袭|坦克|核武|核设施|核打击|核试验|防空|无人机|雷达|"
                            r"集结|炮兵|旅级|营级|"
                            r"military|troop|missile|warplane|warship|drill|airstrike|tank|nuclear|"
                            r"drone|air ?defen[cs]e|radar|artillery|battalion|brigade)", re.I)),
    ("diplomacy", re.compile(r"(外交|大使|召见|制裁|谈判|会谈|磋商|斡旋|峰会|条约|断交|"
                             r"diplomat|ambassador|summon|sanction|negotiat|summit|treaty)", re.I)),
    ("economy", re.compile(r"(关税|出口管制|能源|油价|汇率|供应链|禁运|"
                           r"tariff|export control|oil price|currency|supply chain|embargo)", re.I)),
    ("internal", re.compile(r"(抗议|示威|骚乱|选举|政变|戒严|罢工|"
                            r"protest|riot|election|coup|martial law|strike)", re.I)),
    ("physical", re.compile(r"(航行通告|空域关闭|撤侨|使馆关闭|NOTAM|airspace closed|"
                            r"embassy closed|evacuation|port clos)", re.I)),
]


def severity_from_text(title: str, body: str = "") -> int:
    t = f"{title} {body[:400]}"
    for sev, pat in _SEV_PATTERNS:
        if pat.search(t):
            return sev
    return 1


def component_from_text(title: str, body: str = "") -> str:
    t = f"{title} {body[:400]}"
    for comp, pat in _COMP_PATTERNS:
        if pat.search(t):
            return comp
    return "narrative"


# ---------------------------------------------------------------- 聚合


@dataclass
class RegionScore:
    region: str
    score: float
    z: dict[str, float]


def combine(zvec: dict[str, float], norm: float | None = None) -> float:
    """权重加权求和后映射到 0-100。

    norm 用于"某些分项缺历史"时重新归一化权重，否则缺项会把指数压低。
    """
    total = sum(COMPONENT_WEIGHTS.get(k, 0.0) * v for k, v in zvec.items())
    if norm and norm > 0:
        total = total / norm
    return to_index(total)


def global_index(region_scores: Sequence[float]) -> float:
    """不用均值：max 抓最热的火点，P90 防单点噪声。"""
    if not region_scores:
        return 50.0
    return 0.5 * max(region_scores) + 0.5 * p90(list(region_scores))


def delta_z(current: float, hist: Sequence[float]) -> float | None:
    """今天相对 EWMA 基线偏离几个稳健标准差。"""
    if len(hist) < MIN_HISTORY_DAYS:
        return None
    lam, base = EWMA_LAMBDA, hist[0]
    for v in hist[1:]:
        base = lam * v + (1 - lam) * base
    scale = 1.4826 * mad(hist)
    if scale < 1e-9:
        scale = stdev(hist)
    if scale < 1e-9:
        return 0.0
    return max(-6.0, min(6.0, (current - base) / scale))


def ewma_series(xs: Sequence[float], lam: float = EWMA_LAMBDA) -> list[float]:
    out: list[float] = []
    acc = xs[0] if xs else 0.0
    for i, v in enumerate(xs):
        acc = v if i == 0 else lam * v + (1 - lam) * acc
        out.append(acc)
    return out


# ---------------------------------------------------------------- 源信誉


def update_reputation(hits: int, misses: int, alpha: float = 1.0, beta: float = 3.0) -> float:
    """Beta-Binomial 后验均值。初始悲观（beta 较大），需要战绩来挣。"""
    return (alpha + hits) / (alpha + beta + hits + misses)
