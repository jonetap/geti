"""核心类型、分项定义、源注册表（SWS 先验）。

设计原则：
- 只用标准库，GitHub Actions 上零 pip 安装即可跑
- 所有"权重"都是先验，运行时会被 hit_rate 修正（见 score.update_source_reputation）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

UTC = timezone.utc

# ---------------------------------------------------------------- 分项

COMPONENT_WEIGHTS: dict[str, float] = {
    "military": 0.26,
    "physical": 0.22,
    "diplomacy": 0.18,
    "economy": 0.15,
    "internal": 0.11,
    "narrative": 0.08,
}

COMPONENT_LABELS: dict[str, str] = {
    "military": "军事升级",
    "physical": "物理征兆",
    "diplomacy": "外交",
    "economy": "经济",
    "internal": "内部动荡",
    "narrative": "信息/叙事",
}

# ---------------------------------------------------------------- CAMEO

# GDELT 的 CAMEO 根码 -> (分项, 基准严重度 1-5)
CAMEO_ROOT: dict[str, tuple[str, int]] = {
    "20": ("military", 5),   # USE UNCONVENTIONAL MASS VIOLENCE
    "19": ("military", 5),   # FIGHT
    "18": ("military", 4),   # ASSAULT
    "17": ("military", 4),   # COERCE
    "15": ("military", 3),   # EXHIBIT FORCE POSTURE
    "13": ("military", 3),   # THREATEN
    "16": ("diplomacy", 3),  # REDUCE RELATIONS
    "12": ("diplomacy", 2),  # REJECT
    "11": ("diplomacy", 2),  # DISAPPROVE
    "10": ("diplomacy", 2),  # DEMAND
    "09": ("internal", 2),   # INVESTIGATE
    "14": ("internal", 2),   # PROTEST
    "05": ("diplomacy", 1),  # DIPLOMATIC COOPERATION
    "04": ("diplomacy", 1),  # CONSULT
    "06": ("economy", 1),    # MATERIAL COOPERATION
    "07": ("economy", 1),    # PROVIDE AID
    "08": ("narrative", 1),  # YIELD
    "01": ("narrative", 1),  # MAKE PUBLIC STATEMENT
    "02": ("narrative", 1),  # APPEAL
    "03": ("narrative", 1),  # EXPRESS INTENT TO COOPERATE
}

CAMEO_LABEL: dict[str, str] = {
    "20": "大规模暴力",
    "19": "武装冲突",
    "18": "袭击",
    "17": "胁迫",
    "16": "关系降级/制裁",
    "15": "武力展示",
    "14": "抗议",
    "13": "威胁",
    "12": "拒绝",
    "11": "谴责",
    "10": "要求",
    "09": "调查",
    "08": "让步",
    "07": "援助",
    "06": "实质合作",
    "05": "外交合作",
    "04": "磋商",
    "03": "表达合作意愿",
    "02": "呼吁",
    "01": "公开表态",
}

# ---------------------------------------------------------------- 区域

REGION_LABELS: dict[str, str] = {
    "EAST_ASIA": "东亚",
    "SOUTH_ASIA": "南亚",
    "MIDDLE_EAST": "中东",
    "EUROPE": "欧洲",
    "RUSSIA_CIS": "俄与独联体",
    "AFRICA": "非洲",
    "NORTH_AMERICA": "北美",
    "LATAM": "拉美",
    "OCEANIA": "大洋洲",
    "OTHER": "其他",
}

# FIPS 10-4 国家码 -> 区域（GDELT 的 ActionGeo_CountryCode 用 FIPS，
# 注意 CH=中国、SZ=瑞士、JA=日本、KS=韩国、RS=俄罗斯、UP=乌克兰）
FIPS_REGION: dict[str, str] = {
    "CH": "EAST_ASIA", "JA": "EAST_ASIA", "KS": "EAST_ASIA", "KN": "EAST_ASIA",
    "TW": "EAST_ASIA", "HK": "EAST_ASIA", "MN": "EAST_ASIA",
    "IN": "SOUTH_ASIA", "PK": "SOUTH_ASIA", "BG": "SOUTH_ASIA", "CE": "SOUTH_ASIA",
    "NP": "SOUTH_ASIA", "AF": "SOUTH_ASIA", "LK": "SOUTH_ASIA",
    "IR": "MIDDLE_EAST", "IZ": "MIDDLE_EAST", "IS": "MIDDLE_EAST", "SY": "MIDDLE_EAST",
    "LE": "MIDDLE_EAST", "SA": "MIDDLE_EAST", "TU": "MIDDLE_EAST", "YM": "MIDDLE_EAST",
    "JO": "MIDDLE_EAST", "QA": "MIDDLE_EAST", "AE": "MIDDLE_EAST", "KU": "MIDDLE_EAST",
    "EG": "MIDDLE_EAST", "GZ": "MIDDLE_EAST", "WE": "MIDDLE_EAST",
    "GM": "EUROPE", "FR": "EUROPE", "IT": "EUROPE", "UK": "EUROPE", "SP": "EUROPE",
    "PL": "EUROPE", "NL": "EUROPE", "SW": "EUROPE", "SZ": "EUROPE", "AU": "EUROPE",
    "BE": "EUROPE", "GR": "EUROPE", "HU": "EUROPE", "RO": "EUROPE", "BU": "EUROPE",
    "FI": "EUROPE", "NO": "EUROPE", "DA": "EUROPE", "EI": "EUROPE", "PO": "EUROPE",
    "LO": "EUROPE", "EZ": "EUROPE", "HR": "EUROPE", "RB": "EUROPE", "SR": "EUROPE",
    "RS": "RUSSIA_CIS", "UP": "RUSSIA_CIS", "BO": "RUSSIA_CIS", "KZ": "RUSSIA_CIS",
    "AM": "RUSSIA_CIS", "AJ": "RUSSIA_CIS", "GG": "RUSSIA_CIS", "MD": "RUSSIA_CIS",
    "NG": "AFRICA", "SU": "AFRICA", "KE": "AFRICA", "ET": "AFRICA", "SO": "AFRICA",
    "LY": "AFRICA", "SF": "AFRICA", "ZA": "AFRICA", "CM": "AFRICA", "TZ": "AFRICA",
    "UG": "AFRICA", "CD": "AFRICA", "CG": "AFRICA", "ML": "AFRICA", "NI": "AFRICA",
    "US": "NORTH_AMERICA", "CA": "NORTH_AMERICA", "MX": "LATAM",
    "BR": "LATAM", "AR": "LATAM", "CO": "LATAM", "VE": "LATAM", "PE": "LATAM",
    "CI": "LATAM", "BL": "LATAM", "UY": "LATAM", "EC": "LATAM",
    "AS": "OCEANIA", "NZ": "OCEANIA",
}


def region_of(fips: str) -> str:
    return FIPS_REGION.get((fips or "").upper().strip(), "OTHER")


def cameo_component(code: str) -> tuple[str, int]:
    """CAMEO 事件码 -> (分项, 严重度)。空码返回 narrative/1。

    17（COERCE）整体归军事，但其中 172 制裁 / 173 禁运 / 174 抵制
    是**经济胁迫**——把它们留在 military 里，会让"经济"这个分项在
    真实数据里几乎永远空着，而制裁恰恰是开战前最典型的中间步骤。
    """
    code = (code or "").strip()
    if not code:
        return ("narrative", 1)
    if code[:3] in ("172", "173", "174"):
        return ("economy", 4)
    return CAMEO_ROOT.get(code[:2], ("narrative", 1))


def cameo_label(code: str) -> str:
    return CAMEO_LABEL.get((code or "").strip()[:2], "其他")


# ---------------------------------------------------------------- 源

# 源类别 -> 默认可信度先验（DESIGN.md §5.2）
PRIOR: dict[str, float] = {
    "official": 0.90,
    "thinktank": 0.85,
    "agency": 0.80,
    "media": 0.70,
    "vertical": 0.60,
    "selfmedia": 0.35,
    "aggregator": 0.82,
    "social": 0.30,
    "chat": 0.20,
}


@dataclass(frozen=True)
class SourceSpec:
    """一个信息源。

    credibility        —— 对"事实"的可信度先验，0-1
    intent_credibility —— 对"它想让你相信什么"的可信度（敌对方官媒专用）
    domains            —— 该源权威的分项；空元组表示全领域
    """

    id: str
    label: str
    kind: str
    domains: tuple[str, ...] = ()
    credibility: float | None = None
    intent_credibility: float | None = None

    @property
    def prior(self) -> float:
        return self.credibility if self.credibility is not None else PRIOR.get(self.kind, 0.4)

    def domain_match(self, component: str) -> float:
        if not self.domains:
            return 1.0
        return 1.0 if component in self.domains else 0.6


def _s(sid: str, label: str, kind: str, domains: tuple[str, ...] = (), cred: float | None = None,
       intent: float | None = None) -> SourceSpec:
    return SourceSpec(sid, label, kind, domains, cred, intent)


REGISTRY: dict[str, SourceSpec] = {s.id: s for s in (
    # 通讯社
    _s("reuters", "路透社", "agency", ("military", "diplomacy", "economy")),
    _s("ap", "美联社", "agency", ("military", "diplomacy")),
    _s("afp", "法新社", "agency", ("military", "diplomacy")),
    _s("xinhua", "新华社", "agency", ("diplomacy", "economy")),
    _s("xinhua_world", "新华网国际", "agency", ("diplomacy", "military")),
    _s("people", "人民网", "media", ("diplomacy",)),
    _s("chinanews", "中新网", "agency", ("diplomacy", "internal")),
    _s("chinanews_cn", "中新网国内", "agency", ("internal",)),
    _s("globaltimes", "环球时报", "media", ("military", "narrative")),
    _s("cgtn", "CGTN", "media", ("narrative",)),
    _s("bbc_zh", "BBC 中文", "media", ("narrative",)),
    # 这两个一直在 RSS 列表里，却从没登记过——一直被当成自媒体 0.35 计分。
    # 是新加的"每个 feed 都必须在 REGISTRY 里"那条测试把它们翻出来的。
    _s("france24", "法国 24", "media", ("diplomacy", "narrative")),
    _s("un_news_zh", "联合国新闻", "official", ("diplomacy",)),
    # 严肃媒体
    _s("bbc", "BBC", "media"),
    _s("aljazeera", "半岛电视台", "media"),
    _s("guardian", "卫报", "media"),
    _s("dw", "德国之声", "media"),
    _s("nhk", "NHK", "media"),
    _s("scmp", "南华早报", "media"),
    # 专业机构
    _s("isw", "战争研究所 ISW", "thinktank", ("military",)),
    _s("acled", "ACLED", "thinktank", ("military", "internal")),
    _s("ucdp", "UCDP", "thinktank", ("military",)),
    _s("gdelt", "GDELT", "aggregator", ("military", "diplomacy")),
    # 官方一手
    _s("mfa_cn", "中国外交部", "official", ("diplomacy",), 0.90),
    _s("mod_cn", "中国国防部", "official", ("military",), 0.90),
    _s("state_us", "美国国务院", "official", ("diplomacy",), 0.88),
    _s("dod_us", "美国国防部", "official", ("military",), 0.88),
    _s("un", "联合国", "official", ("diplomacy",), 0.90),
    _s("iaea", "IAEA", "official", ("military",), 0.88),
    # 敌对方官媒（双通道）
    _s("mod_ru", "俄国防部", "official", ("military",), 0.45, 0.90),
    _s("irna", "伊朗国家通讯社", "official", ("diplomacy",), 0.45, 0.88),
    _s("kcna", "朝中社", "official", ("military",), 0.40, 0.85),
    # 社交 / 低权重层
    _s("telegram_osint", "Telegram OSINT 频道", "social", (), 0.25),
    _s("x_osint", "X 开源情报账号", "social", (), 0.22),
    _s("weibo_hot", "微博热搜", "social", (), 0.30),
    _s("group_chat", "群聊转述", "chat", (), 0.20),
)}


def source(sid: str) -> SourceSpec:
    """未知源退化为 selfmedia 先验，而不是报错——抓取永远会遇到新源。"""
    got = REGISTRY.get(sid)
    if got is not None:
        return got
    return SourceSpec(sid, sid, "selfmedia")


# ---------------------------------------------------------------- 记录


@dataclass
class EventSignal:
    """来自结构化事件库（GDELT/UCDP）的信号，不经过 LLM。"""

    day: date
    fips: str
    region: str
    cameo: str
    component: str
    severity: int
    goldstein: float
    mentions: int
    actors: str = ""
    url: str = ""
    eid: str = ""            # GLOBALEVENTID：去重靠它，别靠标题

    def __post_init__(self) -> None:
        self.severity = max(1, min(5, int(self.severity)))


@dataclass
class RawItem:
    """来自新闻源的一条原文。"""

    source_id: str
    title: str
    body: str
    url: str
    published: datetime
    lang: str = "en"


@dataclass
class Cluster:
    """同一事件的多条报道（已按起源归并）。"""

    cid: str
    title: str
    body: str
    url: str
    published: datetime
    items: list[RawItem] = field(default_factory=list)

    @property
    def origins(self) -> list[str]:
        seen: list[str] = []
        for it in self.items:
            if it.source_id not in seen:
                seen.append(it.source_id)
        return seen

    @property
    def n_origin(self) -> int:
        return len(self.origins)


@dataclass
class ScoredCluster:
    cluster: Cluster
    component: str
    severity: int
    confidence: float
    evidence: float
    corroboration: float
    specificity: float
    origin_count: int

    @property
    def summary(self) -> str:
        return f"{self.cluster.title}（{self.origin_count} 源 / 严重度 {self.severity}）"
