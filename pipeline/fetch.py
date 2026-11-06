"""网络层：GDELT 增量文件 + RSS 抓取。

全部用标准库 urllib，GitHub Actions 上不需要 pip install。
注意 runner 在境外，因此不需要代理——这是"零服务器"方案成立的关键。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

from .model import EventSignal, RawItem
from .parse import gdelt_from_zip, parse_feed

UTC = timezone.utc
UA = "geti-monitor/0.1 (+https://github.com/) python-urllib"

GDELT_LASTUPDATE = "https://data.gdeltproject.org/gdeltv2/lastupdate.txt"
GDELT_BASE = "https://data.gdeltproject.org/gdeltv2/"

# 只放"公开、稳定、有全球视角"的源。任何一个失效都不该让整批失败。
RSS_FEEDS: dict[str, tuple[str, str]] = {
    #  source_id                 url                                                    lang
    "bbc": ("https://feeds.bbci.co.uk/news/world/rss.xml", "en"),
    "aljazeera": ("https://www.aljazeera.com/xml/rss/all.xml", "en"),
    "guardian": ("https://www.theguardian.com/world/rss", "en"),
    "dw": ("https://rss.dw.com/rdf/rss-en-world", "en"),
    "france24": ("https://www.france24.com/en/rss", "en"),
    "nhk": ("https://www3.nhk.or.jp/rss/news/cat0.xml", "ja"),
    "scmp": ("https://www.scmp.com/rss/91/feed", "en"),
    "un_news_zh": ("https://news.un.org/feed/subscribe/zh/news/all/rss.xml", "zh"),
    # 中文源：GDELT 对中文报道的覆盖本来就弱，东亚这一块的"我们这边怎么说的"
    # 只能自己抓。注意官媒转载同一条通稿很常见——所以置信度永远按
    # "独立起源"算，不看条数。
    #
    # 已删掉三个源，都是 2026-11-05 查实的死源，留着只会每轮报红：
    #   xinhua_world  条目里没有 pubDate，最新一篇停在 2022-12-14（停更四年的死档案）
    #   people        时间戳停在 2025-06，且与新华网高度重合
    #   globaltimes   50 条全部超过 48 小时（供旧闻）
    "chinanews": ("https://www.chinanews.com.cn/rss/world.xml", "zh"),
    "chinanews_cn": ("https://www.chinanews.com.cn/rss/china.xml", "zh"),
    "bbc_zh": ("https://feeds.bbci.co.uk/zhongwen/simp/rss.xml", "zh"),
}

MAX_ITEM_AGE_HOURS = 48      # 新华社那个 feed 一次吐 300 条，能翻到两周前

ADAPTER_INTERVAL_SEC = {
    "gdelt": 900,
    "rss": 1800,
}


def http_get(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- GDELT


def gdelt_urls(n: int = 1) -> list[str]:
    """读 lastupdate.txt，返回最近 n 个 15 分钟窗口的 export 文件地址。"""
    try:
        text = http_get(GDELT_LASTUPDATE, timeout=20).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return []
    stamp = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "export.CSV.zip" in parts[-1]:
            stamp = parts[-1].rsplit("/", 1)[-1].replace(".export.CSV.zip", "")
            break
    if not stamp or len(stamp) < 14:
        return []
    try:
        t0 = datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return []
    return [
        f"{GDELT_BASE}{(t0 - timedelta(minutes=15 * i)).strftime('%Y%m%d%H%M%S')}.export.CSV.zip"
        for i in range(max(1, n))
    ]


def fetch_gdelt(n_windows: int = 1, keep_regions: set[str] | None = None) -> tuple[list[EventSignal], dict]:
    health = {"source": "gdelt", "ok": False, "files": 0, "error": ""}
    signals: list[EventSignal] = []
    urls = gdelt_urls(n_windows)
    if not urls:
        health["error"] = "lastupdate.txt 不可读"
        return signals, health
    for url in urls:
        try:
            data = http_get(url, timeout=40)
            signals.extend(gdelt_from_zip(data, keep_regions))
            health["files"] += 1
        except Exception as exc:                      # 单个窗口失败不影响其它
            health["error"] = f"{type(exc).__name__}: {exc}"[:200]
    health["ok"] = health["files"] > 0
    return signals, health


# ---------------------------------------------------------------- RSS

def fetch_gdelt_recent(hours: int = 36, workers: int = 8,
                       keep_regions: set[str] | None = None,
                       min_mentions: int = 2) -> tuple[list[EventSignal], list[dict], object]:
    """最近 N 小时的 15 分钟文件（GDELT 2.0）。

    为什么不用 events/YYYYMMDD 日档：日档要等 GDELT 归档，实测滞后 1–2 天，
    页面上会一直写着"数据截止 = 前天"，而首页写着"实时数据"。
    15 分钟文件基本实时（滞后 15–45 分钟），代价是同一事件在多个窗口重复出现，
    必须按 GLOBALEVENTID 去重（去重发生在 build.pressure_from_signals）。
    """
    from concurrent.futures import ThreadPoolExecutor

    urls = gdelt_urls(max(4, int(hours) * 4))
    health = [{"source": "gdelt_recent", "ok": False, "files": 0, "items": 0, "error": ""}]
    if not urls:
        health[0]["error"] = "lastupdate.txt 不可读"
        return [], health, None

    def one(u: str) -> list[EventSignal]:
        try:
            return gdelt_from_zip(http_get(u, timeout=40), keep_regions,
                                  min_mentions=min_mentions)
        except Exception:                       # 单个窗口失败不算整体失败
            return []

    got: list[EventSignal] = []
    files = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for rows in pool.map(one, urls):
            if rows:
                files += 1
                got.extend(rows)
    health[0]["files"] = files
    if not got:
        health[0]["error"] = "窗口内没有解析出事件"
        return got, health, None
    health[0]["ok"] = True
    health[0]["items"] = len(got)
    return got, health, max(ev.day for ev in got)


def fetch_feeds(feeds: dict[str, tuple[str, str]] | None = None) -> tuple[list[RawItem], list[dict]]:
    feeds = feeds or RSS_FEEDS
    items: list[RawItem] = []
    health: list[dict] = []
    now = datetime.now(UTC)
    for sid, (url, lang) in feeds.items():
        rec = {"source": sid, "ok": False, "items": 0, "error": ""}
        try:
            raw = http_get(url, timeout=25)
            parsed = parse_feed(raw.decode("utf-8", errors="replace"), sid, now=now, lang=lang)
            # 没有发布时间的条目一律丢弃。空值不是"现在"，是"不知道"——
            # 而一个不知道时间的条目，无法支撑"突发"这个语义（见 §21 的事故）。
            undated = [it for it in parsed if it.published is None]
            dated = [it for it in parsed if it.published is not None]
            got = [it for it in dated
                   if (now - it.published).total_seconds() <= MAX_ITEM_AGE_HOURS * 3600]
            items.extend(got)
            rec["items"] = len(got)
            rec["undated"] = len(undated)
            rec["ok"] = bool(got)
            if undated and not dated:
                rec["error"] = f"{len(undated)} 条全无发布时间，无法判断新鲜度"
            elif not got and dated:
                rec["error"] = f"解析到 {len(dated)} 条，全部超过 {MAX_ITEM_AGE_HOURS} 小时"
            elif not got:
                rec["error"] = "解析到 0 条（可能改版）"
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        health.append(rec)
    return items, health


# ---------------------------------------------------------------- 健康


def load_health(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_health(path: str, records: list[dict], now: datetime | None = None) -> None:
    now = now or datetime.now(UTC)
    state = load_health(path)
    for rec in records:
        sid = rec["source"]
        prev = state.get(sid, {})
        entry = {
            "last_success_at": now.isoformat(timespec="seconds") if rec.get("ok") else prev.get("last_success_at"),
            "last_attempt_at": now.isoformat(timespec="seconds"),
            "last_error": "" if rec.get("ok") else rec.get("error", ""),
            "consecutive_failures": 0 if rec.get("ok") else int(prev.get("consecutive_failures", 0)) + 1,
        }
        state[sid] = entry
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 按日文件

GDELT_DAILY = "https://data.gdeltproject.org/events/{stamp}.export.CSV.zip"


def _get_retry(url: str, timeout: int = 120, attempts: int = 3) -> bytes:
    """带重试的 GET。404 不重试（文件确实不存在），其余重试两次。"""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return http_get(url, timeout=timeout)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                raise
            last = exc
        except Exception as exc:                      # 超时/连接重置/5xx
            last = exc
        if i < attempts - 1:
            time.sleep(2 + 3 * i)
    raise last if last else RuntimeError("unreachable")


def fetch_gdelt_daily(days: int = 2, end=None, max_lookback: int = 6,
                      keep_regions: set[str] | None = None,
                      min_mentions: int = 2) -> tuple[list[EventSignal], list[dict], object]:
    """按日增量文件（每个 ~10MB），一天一个。

    用"日文件 + 整日重算"而不是"15 分钟文件 + 累加"，原因是后者在无状态
    CI 里会重复计数或漏计；日文件可以幂等覆盖，跑多少次结果都一样。

    **必须往前回退**：当天的文件往往还没发布（实测常年 404），拿半天数据
    算当日指数会系统性偏低。返回值第三项 `resolved_date` 是"实际解析到的
    最新一天"，调用方必须用它当快照的 target，不能用 now()。
    """
    end = end or datetime.now(UTC).date()
    want = max(1, days)
    signals: list[EventSignal] = []
    health: list[dict] = []
    got_days = []
    looked = 0
    budget = want + max(0, max_lookback)

    while len(got_days) < want and looked < budget:
        d = end - timedelta(days=looked)
        looked += 1
        rec = {"source": f"gdelt_daily_{d.isoformat()}", "ok": False, "items": 0, "error": ""}
        try:
            data = _get_retry(GDELT_DAILY.format(stamp=d.strftime("%Y%m%d")))
        except urllib.error.HTTPError as exc:
            rec["error"] = f"HTTP {exc.code}" + ("（文件尚未发布）" if exc.code == 404 else "")
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:200]
        else:
            try:
                got = gdelt_from_zip(data, keep_regions, min_mentions=min_mentions)
            except Exception as exc:
                rec["error"] = f"解析失败 {type(exc).__name__}: {exc}"[:200]
            else:
                signals.extend(got)
                rec["items"] = len(got)
                rec["ok"] = True
                if not got:
                    rec["error"] = "解析到 0 条（格式可能变化）"
                got_days.append(d)
        health.append(rec)

    resolved = max(got_days) if got_days else None
    return signals, health, resolved


def parse_feed_items(xml: str) -> list:
    """RSS/Atom → [{"t": 标题, "d": 时间, "u": 链接}]，供按日期筛选。

    时间字段三种写法都吃：RFC822（pubDate）、ISO（published/updated）。
    解析不出来就给 None——调用方自己决定是"算今天"还是"丢掉"。
    """
    from email.utils import parsedate_to_datetime

    out: list = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return out
    for el in root.iter():
        if el.tag.split("}")[-1] not in ("item", "entry"):
            continue
        title = None
        when = None
        link = None
        for ch in el:
            name = ch.tag.split("}")[-1].lower()
            txt = (ch.text or "").strip()
            if name == "title" and txt and title is None:
                title = txt
            elif name == "link" and txt and not link:
                link = txt
            elif name in ("pubdate", "published", "updated", "date") and txt:
                try:
                    when = parsedate_to_datetime(txt)
                except (TypeError, ValueError):
                    try:
                        when = datetime.fromisoformat(txt.replace("Z", "+00:00"))
                    except ValueError:
                        when = None
        if title:
            out.append({"t": title, "d": when, "u": link or ""})
    return out


def on_day(when, day) -> bool:
    """时间是否落在 day 当天（UTC）。None 视为未知，放行。"""
    if when is None:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc).date() == day
