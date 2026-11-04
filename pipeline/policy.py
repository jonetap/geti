"""政策脉冲：把"政策"当成背景层，不塞进 GETI。

为什么单独一层——因为**政策是原因，抗议是结果**。把"英国通过强制社保
法案"和"伦敦十万人上街"放进同一个数字里，等于把火和烟算成一件事：
真出事那天，两个信号会互相抵消成一团模糊的中间值。

v2 修的是**误判**，不是公式。上线第一版把三条中国反腐新闻（"接受审查
调查""提起公诉"）和一条菲律宾货币政策判成了政策脉冲——教训是：**光靠
动词（审查、紧缩、起诉）识别政策必错**。必须先确认"这是一项对公众生效
的规则"，再看它是不是强制的。
"""

import re
from datetime import date

# ---------------------------------------------------------------- 门槛

# 必须出现"政策工具"名词，否则一律不算——这条门槛挡掉了绝大部分误判
INSTRUMENT = re.compile(
    r"法案|政策|条例|规定|办法|措施|改革|制度|社保|医保|养老|抚恤|补贴|救助|"
    r"税|费率|缴费|征缴|禁令|管制|配额|户籍|征兵|宵禁|紧急状态|"
    r"\bbill\b|\blaw\b|\bact\b|policy|regulation|rule|mandate|levy|tax|subsid|"
    r"fee|charge|curfew|conscription|quota|licen[cs]e|welfare|pension|"
    r"insurance|contribution",
    re.I)

# 司法/纪检个案：**不归类、不打分，但保留**——个案也是客观事件，
# 只是它反映的不是"政策走向"，所以放在 notable 里单独展示。
EXCLUDE_JUSTICE = re.compile(
    r"受贿|行贿|贪污|挪用|纪律审查|立案审查|审查调查|双开|落马|被查|"
    r"公诉|起诉|判决|判处|有期徒刑|无期徒刑|涉嫌|纪委|监委|纪检|巡视组|"
    r"indicted|convicted|sentenced|corruption|bribery",
    re.I)

# 货币政策：连"参考"都不算——利率和流动性不是对公民的规则，混进来只会稀释
EXCLUDE_MONEY = re.compile(
    r"货币政策|央行|准备金率|加息|降息|通胀|通货膨胀|利率决议|"
    r"monetary policy|central bank|inflation|interest rate",
    re.I)

EXCLUDE = re.compile(EXCLUDE_JUSTICE.pattern + "|" + EXCLUDE_MONEY.pattern, re.I)

# ---------------------------------------------------------------- 类型

EXTRACTIVE = re.compile(
    # 中文里名词常夹在动词中间："强制**所有公民**缴纳社保"
    r"强制[^。；，]{0,10}(?:参保|缴纳|缴费|投保|购买)|"
    r"加税|增税|提高费率|上调费率|"
    r"(?:削减|取消|下调|停发)[^。；，]{0,10}补贴|"
    r"延迟退休|提高退休年龄|资本管制|外汇管制|征用|追缴|财政紧缩|紧缩措施|福利削减|"
    r"mandatory (?:contribution|insurance|enrolment|enrollment)|"
    r"compulsory (?:insurance|contribution|saving|participation)|"
    r"tax (?:hike|rise|increase)|capital controls|austerity|"
    r"(?:cut|scrap|slash|reduce|means[- ]test)[^.]{0,20}subsid(?:y|ies)|"
    r"levy a fine|forced saving",
    re.I)

CONTROL = re.compile(
    r"紧急状态|宵禁|戒严|征兵|封控|限制出境|禁止出境|出境禁令|言论管控|"
    r"网络管制|断网|军事管制|"
    r"state of emergency|curfew|conscription|martial law|lockdown|"
    r"exit ban|travel ban|censorship|emergency powers|mobilization order",
    re.I)

REDISTRIBUTIVE = re.compile(
    r"发放补贴|提高补贴|减税|福利扩大|扩大福利|最低收入|全民基本收入|"
    r"tax cut|subsid(?:y|ies)[^.]{0,20}(?:boost|increase|rise)|"
    r"(?:boost|increase|raise|expand)[^.]{0,20}subsid(?:y|ies)|stimulus|"
    r"universal basic income|benefit (?:rise|increase)|welfare expansion",
    re.I)

# 只描述程序、没改变谁的日子——丢掉
PROCEDURAL = re.compile(
    r"first reading|second reading|referral to committee|"
    r"一读|二读|交付委员会|征求意见稿", re.I)

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
    "阿富汗": "AF", "afghanistan": "AF",
    "阿根廷": "AR", "argentina": "AR",
    "墨西哥": "MX", "mexico": "MX",
    "加拿大": "CA", "canada": "CA",
    "澳大利亚": "AU", "australia": "AU",
    "意大利": "IT", "italy": "IT",
    "西班牙": "ES", "spain": "ES",
    "波兰": "PL", "poland": "PL",
    "乌克兰": "UA", "ukraine": "UA",
    "荷兰": "NL", "netherlands": "NL",
    "瑞典": "SE", "sweden": "SE",
    "挪威": "NO", "norway": "NO",
    "瑞士": "CH", "switzerland": "CH",
    "希腊": "GR", "greece": "GR",
    "匈牙利": "HU", "hungary": "HU",
    "菲律宾": "PH", "philippines": "PH",
    "越南": "VN", "vietnam": "VN",
    "泰国": "TH", "thailand": "TH",
    "印尼": "ID", "印度尼西亚": "ID", "indonesia": "ID",
    "马来西亚": "MY", "malaysia": "MY",
    "新加坡": "SG", "singapore": "SG",
    "沙特": "SA", "saudi": "SA",
    "阿联酋": "AE", "emirates": "AE",
    "伊拉克": "IQ", "iraq": "IQ",
    "叙利亚": "SY", "syria": "SY",
    "孟加拉": "BD", "bangladesh": "BD",
    "斯里兰卡": "LK", "sri lanka": "LK",
    "尼泊尔": "NP", "nepal": "NP",
    "哈萨克斯坦": "KZ", "kazakhstan": "KZ",
    "埃塞俄比亚": "ET", "ethiopia": "ET",
    "肯尼亚": "KE", "kenya": "KE",
    "摩洛哥": "MA", "morocco": "MA",
    "智利": "CL", "chile": "CL",
    "秘鲁": "PE", "peru": "PE",
    "委内瑞拉": "VE", "venezuela": "VE",
    "台湾": "TW", "taiwan": "TW",
    "香港": "HK", "hong kong": "HK",
}

MAX_ITEMS = 12
MAX_NOTABLE = 6


def classify(text: str) -> str | None:
    """返回 extractive / control / redistributive / None。

    顺序很重要：先排除个案与货币政策，再要求"政策工具"名词，
    最后才看类型——反过来做（先看动词）就是 v1 栽跟头的原因。
    """
    if not text or EXCLUDE.search(text) or PROCEDURAL.search(text):
        return None
    if CONTROL.search(text):
        return "control"
    if not INSTRUMENT.search(text):
        return None
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
    notable = []
    counts = {"extractive": 0, "control": 0, "redistributive": 0, "notable": 0}
    pressure: dict[str, float] = {}

    for cl in clusters or []:
        text = f"{cl.title} {cl.body or ''}"[:1200]

        # 个案：不分类、不计分，但保留成"客观事件"给人看
        if EXCLUDE_JUSTICE.search(text) and not EXCLUDE_MONEY.search(text):
            first = cl.items[0] if getattr(cl, "items", None) else None
            notable.append({
                "kind": "judicial",
                "title": cl.title[:120],
                "url": cl.url or "",
                "country": country_of(text),
                "source": getattr(first, "source_id", "") if first else "",
                "when": (cl.published.date().isoformat() if cl.published
                         else (day.isoformat() if day else "")),
            })
            counts["notable"] += 1
            continue

        kind = classify(text)
        if kind is None:
            continue
        sc = intensity(text, kind)
        iso = country_of(text)
        signed = sc if kind in ("extractive", "control") else -sc
        counts[kind] += 1
        if iso:
            pressure[iso] = round(pressure.get(iso, 0.0) + signed, 3)
        first = cl.items[0] if getattr(cl, "items", None) else None
        items.append({
            "type": kind,
            "country": iso,
            "source": getattr(first, "source_id", "") if first else "",
            "score": sc,
            "signed": signed,
            "title": cl.title[:120],
            "url": cl.url or "",
            "origins": cl.n_origin,
            "when": (cl.published.date().isoformat() if cl.published
                     else (day.isoformat() if day else "")),
        })

    items.sort(key=lambda x: (abs(x["signed"]), x["origins"]), reverse=True)
    notable.sort(key=lambda x: x["when"], reverse=True)
    return {
        "items": items[:MAX_ITEMS],
        "notable": notable[:MAX_NOTABLE],
        "counts": counts,
        "pressure": dict(sorted(pressure.items(), key=lambda kv: -abs(kv[1]))),
        "note": "政策是原因、抗议是结果，这一层只做背景，不参与 GETI 计算。个案列为客观参考，不归类也不计分。",
        "mode": "shadow",
    }
