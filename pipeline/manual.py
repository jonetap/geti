"""人工线索：把"人看到但机器没抓到"的东西接进来。

**这一层不计分。** 理由有三条，任何一条都够：

1. 一个人可以反复提交同一条——刷分比抓取干净得多，防不住；
2. 提交来的链接无法验证是不是转载，而独立起源数是我们整套加权的基石；
3. 人工提交的价值本来就不在"加分"，在**暴露盲区**：如果一条链接人提交了、
   抓取器却没抓到，那说明我们的源漏了它。这个比例才是这一层真正的产出。

所以它进快照、进页面，但不进 GETI。跟政策个案同一个待遇：看得见，不动数字。

后端用 GitHub Issue 当提交箱——不是因为它好，是因为它零成本：
公开仓库的 issue 匿名可读（不需要 token），自带身份与时间戳，还顺手留了审计痕迹。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com/repos/{repo}/issues?labels={label}&state=all&per_page=50"
DEFAULT_REPO = "jonetap/geti"
LABEL = "intel"
MAX_ITEMS = 20
KEEP_DAYS = 30

_URL = re.compile(r"https?://[^\s<>()\[\]\"']+")
_IMG_EXT = re.compile(r"\.(?:png|jpe?g|gif|webp)(?:\?|$)", re.I)
_IMG_MD = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
_NOTE_HEAD = re.compile(r"^#{1,4}\s*一句话[^\n]*$", re.M)
# issue 模板里的提示语，用户没改就原样留在正文里。它不是"说明"，是占位符。
_PLACEHOLDER = re.compile(r"^[（(][^）)]{0,120}[）)]$")

IMAGE_DIR = "web/intel"
IMAGE_BASE = "intel"
MAX_IMAGE_BYTES = 3 * 1024 * 1024


def _clean_url(u: str) -> str:
    return u.rstrip(").,，。；;、")


def parse_issue(issue: dict) -> dict | None:
    """把一个 issue 摊成一条线索。识别失败返回 None，绝不抛。"""
    if not isinstance(issue, dict) or issue.get("pull_request"):
        return None                      # PR 也会出现在 issues 接口里，排除
    body = issue.get("body") or ""
    title = (issue.get("title") or "").strip()

    images = [_clean_url(u) for u in _IMG_MD.findall(body)]
    images += [_clean_url(u) for u in _URL.findall(body)
               if "user-attachments/assets" in u or _IMG_EXT.search(u)]
    links = [_clean_url(u) for u in _URL.findall(body)
             if u not in images and "user-attachments/assets" not in u]

    # 去重但保序
    seen: set[str] = set()
    images = [u for u in images if not (u in seen or seen.add(u))]
    links = [u for u in links if not (u in seen or seen.add(u))]

    note = ""
    m = _NOTE_HEAD.search(body)
    if m:
        rest = body[m.end():].lstrip("\n")
        for line in rest.splitlines():
            s = line.strip().lstrip("-*").strip()
            if s and not _URL.search(s) and not s.startswith("#") \
                    and not _PLACEHOLDER.match(s):
                note = s
                break
    if not note:
        for line in body.splitlines():
            s = line.strip().lstrip("-*").strip()
            if s and not _URL.search(s) and not s.startswith("#") \
                    and not _PLACEHOLDER.match(s) and not s.startswith("###"):
                note = s
                break

    created = issue.get("created_at") or ""
    if not (title or note or links or images):
        return None
    kind = "image" if images and not links else ("link" if links else "text")
    return {
        "number": issue.get("number"),
        "title": title[:140] or (note[:140] if note else "（无标题线索）"),
        "note": note[:280],
        "url": issue.get("html_url") or "",
        "author": (issue.get("user") or {}).get("login") or "",
        "created": created,
        "links": links[:4],
        "images": images[:3],
        "kind": kind,
    }


def parse_issues(payload) -> list[dict]:
    if isinstance(payload, dict):        # 出错时 GitHub 返回 {"message": ...}
        return []
    out = []
    for it in payload or []:
        row = parse_issue(it)
        if row:
            out.append(row)
    return out


def _norm(u: str) -> str:
    """只留 host + path，用来跟抓取到的链接做粗比对。"""
    m = re.match(r"https?://([^/]+)(/[^?#]*)", u or "")
    if not m:
        return ""
    host = m.group(1).lower().removeprefix("www.")
    return host + m.group(2).rstrip("/").lower()


def mark_seen(rows: list[dict], items) -> list[dict]:
    """标出"这条人提交了、但抓取器没抓到"——那才是这一层的信息。"""
    have = {_norm(getattr(it, "url", "")) for it in items or []}
    have.discard("")
    for r in rows:
        hit = any(_norm(u) in have for u in r.get("links") or [])
        r["seen"] = bool(hit)
    return rows


def _download(url: str, timeout: int = 20) -> tuple[bytes, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "geti/0.5"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.headers.get("Content-Type", "") or ""


def _ext_of(url: str, ctype: str) -> str:
    m = re.search(r"\.(png|jpe?g|gif|webp)(?:\?|$)", url, re.I)
    if m:
        return "." + m.group(1).lower().replace("jpeg", "jpg")
    for ext in ("png", "gif", "webp", "jpeg", "jpg"):
        if ext in ctype:
            return "." + ext.replace("jpeg", "jpg")
    return ".jpg"


def localize_images(rows, out_dir: str = IMAGE_DIR, prefix: str = IMAGE_BASE,
                    timeout: int = 20):
    """把截图存进仓库再引用。

    为什么要这一步：issue 里粘的图是 GitHub 的临时图床链接，过一段时间就会失效，
    页面上的缩略图会变成碎图——而线索本身是长期留存的。下载一次、按 URL 哈希命名，
    重复提交同一张图不会产生第二份文件。下载失败就保留原链接，不让线索丢掉。
    """
    os.makedirs(out_dir, exist_ok=True)
    for r in rows:
        fixed: list[str] = []
        for u in r.get("images") or []:
            if u.startswith(prefix + "/"):
                fixed.append(u)
                continue
            try:
                data, ctype = _download(u, timeout)
                if not data or len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("empty or too large")
                name = hashlib.sha1(u.encode("utf-8")).hexdigest()[:16] + _ext_of(u, ctype)
                path = os.path.join(out_dir, name)
                if not os.path.exists(path):
                    with open(path, "wb") as fh:
                        fh.write(data)
                fixed.append(f"{prefix}/{name}")
            except Exception:                    # noqa: BLE001
                fixed.append(u)
        r["images"] = fixed
    return rows


def fetch_issues(repo: str = "", label: str = LABEL, timeout: int = 30):
    """读 issue 列表。公开仓库不需要 token；有 token 就用（配额更高）。"""
    repo = repo or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    req = urllib.request.Request(
        API.format(repo=repo, label=label),
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "geti/0.5",
                 **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def build_block(items, now: datetime | None = None, repo: str = "",
                out_dir: str | None = None) -> tuple[dict, dict]:
    """返回 (manual 块, 健康记录)。任何异常都退化成空块，绝不阻断主流程。"""
    now = now or datetime.now(timezone.utc)
    health = {"source": f"manual:{LABEL}", "ok": False, "items": 0}
    try:
        rows = parse_issues(fetch_issues(repo))
        health["ok"] = True
    except Exception as exc:                     # noqa: BLE001
        err = f"{type(exc).__name__}: {exc}"
        health["error"] = err[:200]
        # 失败要说清是哪一种失败：读不到（网络/接口/版本不一致）和"没人提交"
        # 是两件事，前者需要修，后者什么都不用做。曾经的静默降级让一个
        # 版本不一致的 TypeError 藏了几天。
        return {"items": [], "note": f"读取人工线索失败：{err[:120]}（不影响指数）",
                "mode": "failed"}, health

    cutoff = (now - timedelta(days=KEEP_DAYS)).date()
    keep = []
    for r in rows:
        try:
            d = datetime.fromisoformat(r["created"].replace("Z", "+00:00")).date()
        except Exception:                        # noqa: BLE001
            d = None
        if d and d < cutoff:
            continue
        keep.append(r)
    keep = mark_seen(keep, items)[:MAX_ITEMS]
    keep.sort(key=lambda x: x.get("created") or "", reverse=True)
    if out_dir:
        try:
            localize_images(keep, out_dir=out_dir)
        except Exception:                        # noqa: BLE001
            pass                                 # 存图失败不影响线索本身
    unseen = sum(1 for r in keep if not r.get("seen"))
    health["items"] = len(keep)
    return {
        "items": keep,
        "unseen": unseen,
        "note": (f"人工提交 {len(keep)} 条，其中 {unseen} 条抓取器没有收录——"
                 "这类条目也可能是我们的盲区。本层不计分。"),
        "mode": "shadow",
    }, health
