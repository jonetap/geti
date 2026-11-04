"""疫情层：官方通报 + 政策信号 + 报道密度。

要"数值 + 图表 + 信源"，不做流行病学建模。三条腿各有分工：
  * 官方通报（WHO DON）——最权威，但滞后，天然稀疏；
  * 政策信号（PHEIC 声明、旅行限制、封锁）——**政策是反应，不是风险本身**，
    但它一旦出现，说明有人已经按最坏情况在准备，所以单独计权；
  * 报道密度——最新最快，也最容易被一个国家的新闻周期带偏，所以只占三成。

WHO 的 feed 地址换过几次，所以这里给候选列表逐个尝试；全部失败就如实记 ERR，
不让疫情层静默变成 0。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import date

from .fetch import _get_retry, on_day, parse_feed_items

WHO_FEEDS = [
    "https://www.who.int/feeds/entity/csr/don/en/rss.xml",
    "https://www.who.int/rss-feeds/news-english.xml",
    "https://www.ecdc.europa.eu/en/taxonomy/term/1/rss.xml",
]

# 疾病名：命中即算"疫情相关报道"。用具体病名而不是 covid/pandemic 这种大词，
# 否则任何一篇讲"后疫情时代经济"的文章都会把密度抬起来。
DISEASE = re.compile(
    r"(?<![后前])疫情|传染病|确诊|感染|人传人|爆发|变异株|"
    r"禽流感|H5N1|H7N9|埃博拉|马尔堡|霍乱|麻疹|猴痘|登革热|"
    r"脊髓灰质炎|小儿麻痹|炭疽|鼠疫|拉沙热|尼帕|"
    r"outbreak|epidemic|pandemic|avian (?:flu|influenza)|"
    r"ebola|marburg|cholera|measles|mpox|monkeypox|dengue|"
    r"polio|plague|anthrax|nipah|lassa",
    re.I,
)
POLICY = re.compile(
    r"PHEIC|国际关注的突发公共卫生事件|"
    r"旅行限制|入境限制|边境关闭|停飞|封锁|宵禁|隔离|"
    r"travel (?:ban|restriction)|border clos|lockdown|quarantine|"
    r"state of emergency|公共卫生紧急状态",
    re.I,
)

WEIGHTS = {
    "who_don": 0.40,
    "policy": 0.30,
    "coverage": 0.30,
}
LABELS = {
    "who_don": "官方通报",
    "policy": "政策信号",
    "coverage": "报道密度",
}


def parse_feed_titles(text: str) -> list[str]:
    """只取标题——疫情层不需要正文。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    titles = []
    for item in root.iter():
        if item.tag.split("}")[-1] != "item":
            continue
        for child in item:
            if child.tag.split("}")[-1].lower() == "title" and (child.text or "").strip():
                titles.append(child.text.strip())
                break
    return titles


def fetch_day(day: date, news_items=None) -> tuple[dict, list[dict]]:
    """返回 (读数, health)。

    `news_items` 是主流程已经抓到的新闻（`RawItem` 列表），传进来就不用重复抓。
    没有的话疫情层只靠官方 feed，报道密度分项留空——**缺数据就缺，不补零**。
    """
    out: dict[str, float] = {}
    health: list[dict] = []

    def rec(source: str, ok: bool, items: int, error: str = "") -> None:
        health.append({"source": source, "ok": ok, "items": items, "error": error[:200]})

    titles: list[str] = []
    who_items: list[dict] = []
    don_count = 0
    feed_ok = False
    tried = 0
    last_err = "全部候选地址失败"
    for url in WHO_FEEDS:
        tried += 1
        try:
            data = _get_retry(url, timeout=45)
        except Exception as exc:                                # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        pairs = parse_feed_items(data)
        # 关键：feed 是历史存档（一次 80+ 条），不按日期过滤会得到一个几乎恒定的
        # 大数字，z 恒为 0——看着有数据，其实什么都没测。
        if pairs:
            today = [p for p in pairs if on_day(p["d"], day)]
            titles = [p["t"] for p in today]
            don_count = sum(1 for t in titles if DISEASE.search(t))
            who_items = [{"t": p["t"][:80], "v": "通报", "ts": "", "u": p["u"]}
                         for p in today if DISEASE.search(p["t"])][:10]
            feed_ok = True
            rec("who:don", True, len(titles))
            break
    else:
        rec("who:don", False, 0, last_err if tried else "未配置")

    if feed_ok:
        # 今天没有疫情通报 = 0，这是信息；抓取失败才是缺失，不给 0
        out["who_don"] = float(don_count)
    # 政策信号优先用官方 feed 的标题；没有再退到新闻池
    corpus = [t for t in titles]
    if news_items:
        corpus += [(i.title or "") for i in news_items]
    if corpus:
        hits = [t for t in corpus if POLICY.search(t)]
        for t in hits[:8]:
            who_items.append({"t": str(t)[:80], "v": "政策", "ts": "", "u": ""})
        out["policy"] = float(len(hits))
        out["coverage"] = round(
            sum(1 for t in corpus if DISEASE.search(t)) / max(1, len(corpus)) * 100.0, 2)
    return out, health, {"who": who_items[:12]}
