"""政策脉冲：把"政策"当成背景层，不塞进 GETI。

为什么单独一层——因为**政策是原因，抗议是结果**。把"英国通过强制社保
法案"和"伦敦十万人上街"放进同一个数字里，等于把火和烟算成一件事：
真出事那天，两个信号会互相抵消成一团模糊的中间值。

所以这里只做三件事：
1. 从今天的新闻里挑出"政策类"条目（不是所有法案，是**改变普通人处境**的）；
2. 按类型给符号——提取型/管控型是压力，再分配型是缓和，程序性的丢掉；
3. 按国家汇总成一个"政策脉冲"，**只展示、不计分**，等积累够数据再回测
   "政策 → 30 天内 internal 有没有真的涨"。

判据是"强制性"而不是"重要性"：一个政府宣布"必须"、"违者"、"立即"，
说明它准备用强制力推；宣布"将"、"拟"、"考虑"，那还是纸面。
"""

import re
from datetime import date

# ---------------------------------------------------------------- 词表

EXTRACTIVE = re.compile(
    # 中文里名词常夹在动词中间："强制**所有公民**缴纳社保"——所以用 {0,10} 兜住
    r"强制[^。；，]{0,10}(?:参保|缴纳|缴费|投保|购买)|"
    r"加税|增税|提高费率|上调费率|"
    r"(?:削减|取消|下调|停发)[^。；，]{0,10}补贴|"
    r"资本管制|外汇管制|征用|罚款|追缴|紧缩|福利削减|"
    r"mandatory (?:contribution|insurance|enrolment|enrollment)|"
    r"compulsory (?:insurance|contribution|saving|participation)|"
    r"tax (?:hike|rise|increase)|capital controls|austerity|"
    r"(?:cut|scrap|slash|reduce|means[- ]test)[^.]{0,20}subsid(?:y|ies)|"
    r"means[- ]test(?:ed|ing)|levy a fine|forced saving",
    re.I)

CONTROL = re.compile(
    r"紧急状态|宵禁|戒严|征兵|封控|限制出境|禁止出境|言论管控|审查|"
    r"state of emergency|curfew|conscription|martial law|lockdown|"
    r"exit ban|travel ban|censorship|emergency powers|mobilization order",
    re.I)

REDISTRIBUTIVE = re.compile(
    r"发放补贴|提高补贴|减税|福利扩大|扩大福利|最低收入|全民基本收入|"
    r"tax cut|subsid(?:y|ies)[^.]{0,20}(?:boost|increase|rise)|"
    r"(?:boost|increase|raise|expand)[^.]{0,20}subsid(?:y|ies)|stimulus|universal basic income|"
    r"benefit (?:rise|increase)|welfare expansion",
    re.I)

# 只描述程序、没改变谁的日子——丢掉
PROCEDURAL = re.compile(
    r"first reading|second reading|referral to committee|"
    r"一读|二读|交付委员会|征求意见稿",
    re.I)

MANDATORY = re.compile(
    r"必须|强制|违者|拒不|依法处罚|立即生效|即日|"
    r"must|required to|compulsory|mandatory|penalt(?:y|ies)|"
    r"immediately|effective at once", re.I)

UNIVERSAL = re.compile(
    r"全体|所有[^。；，]{0,10}公民|每个公民|全国|"
    r"all citizens|every adult|nationwide", re.I)

COUNTRIES = {
    "英国": "GB", "united kingdom": "GB", "britain": "GB", "uk ": "GB",
    "美国": "US", "united states": "US", "washington": "US",
    "中国": "CN", "china": "CN", "北京": "CN",
    "日本": "JP", "japan": "JP", "tokyo": "JP",
    "韩国": "KR", "south korea": "KR",
    "德国": "DE", "germany": "DE",
    "法国": "FR", "france": "FR",
    "俄罗斯": "RU", "russia": "RU",
    "印度": "IN", "india": "IN",
    "巴西": "BR", "brazil": "BR",
    "伊朗": "IR", "iran": "IR",
    "以色列": "IL", "israel": "IL",
    "土耳其": "TR", "turkey": "TR",
    "埃及": "EG", "egypt": "EG",
    "尼日利亚": "NG", "nigeria": "NG",
    "南非": "ZA", "south africa": "ZA",
    "巴基斯坦": "PK", "pakistan": "PK",
    "阿根廷": "AR", "argentina": "AR",
    "墨西哥": "MX", "mexico": "MX",
    "加拿大": "CA", "canada": "CA",
    "澳大利亚": "AU", "australia": "AU",
    "意大利": "IT", "italy": "IT",
    "西班牙": "ES", "spain": "ES",
    "波兰": "PL", "poland": "PL",
    "乌克兰": "UA", "ukraine": "UA",
    "台湾": "TW", "taiwan": "TW",
    "香港": "HK", "hong kong": "HK",
}

MAX_ITEMS = 12


def classify(text: str) -> str | None:
    """返回 extractive / control / redistributive / None。"""
    if not text or PROCEDURAL.search(text):
        return None
    if CONTROL.search(text):
        return "control"
    if EXTRACTIVE.search(text):
        return "extractive"
    if REDISTRIBUTIVE.search(text):
        return "redistributive"
    return None


def intensity(text: str, kind: str) -> float:
    """0.2–1.0。强制性越强、覆盖面越大、罚则越重，越接近 1。"""
    s = 0.30 if kind in ("extractive", "control") else 0.20
    if MANDATORY.search(text):
        s += 0.35
    if UNIVERSAL.search(text):
        s += 0.25
    if re.search(r"罚|监禁|违法|penalt|prison|jail|fine", text, re.I):
        s += 0.15
    return round(min(s, 1.0), 3)


def country_of(text: str) -> str:
    low = text.lower()
    for name, iso in COUNTRIES.items():
        if name in low:
            return iso
    return ""


def scan(clusters, day: date | None = None) -> dict:
    """从今天的事件簇里挑政策条目，按国家汇总成脉冲。

    **只展示，不进 GETI**：政策到动荡有周甚至月的滞后，把它塞进日频指数，
    等于用一个月的猜测去污染每天的读数。
    """
    items = []
    counts = {"extractive": 0, "control": 0, "redistributive": 0}
    pressure: dict[str, float] = {}

    for cl in clusters or []:
        text = f"{cl.title} {cl.body or ''}"[:1200]
        kind = classify(text)
        if kind is None:
            continue
        sc = intensity(text, kind)
        iso = country_of(text)
        signed = sc if kind in ("extractive", "control") else -sc
        counts[kind] += 1
        if iso:
            pressure[iso] = round(pressure.get(iso, 0.0) + signed, 3)
        items.append({
            "type": kind,
            "country": iso,
            "score": sc,
            "signed": signed,
            "title": cl.title[:120],
            "url": cl.url or "",
            "origins": cl.n_origin,
            "when": (cl.published.date().isoformat() if cl.published
                     else (day.isoformat() if day else "")),
        })

    items.sort(key=lambda x: (abs(x["signed"]), x["origins"]), reverse=True)
    return {
        "items": items[:MAX_ITEMS],
        "counts": counts,
        "pressure": dict(sorted(pressure.items(), key=lambda kv: -abs(kv[1]))),
        "note": "政策是原因、抗议是结果，这一层只做背景，不参与 GETI 计算。",
        "mode": "shadow",
    }
