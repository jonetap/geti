"""物理征兆：空域活动量。

为什么是"份额"而不是"架次"：一次快照只拍到一瞬间，全球民航的昼夜周期
（同一时刻欧洲夜里、亚洲白天）比任何地区冲突的幅度都大。用某区域占全球
在飞飞机的比例，昼夜被除掉，剩下的才是"这块空域是不是不正常地空了"。

数据本身不区分军用民用——所以只看**骤降**：民航绕飞、空域关闭、撤侨时，
那里的飞机是真的会没的；而"活动变多"假日和军事演习都会变多，分不出来，
就不猜。因此写进序列的是 `-share`：份额塌下去 → robust_z 变正 → 压力上升。
"""

import json
import urllib.request
from datetime import date

URL = "https://opensky-network.org/api/states/all"
UA = {"User-Agent": "geti/0.1 (+https://github.com/jonetap/geti)"}

# 粗粒度的区域框，(lat_min, lat_max, lon_min, lon_max)。重叠无所谓，只看趋势。
REGION_BOXES = {
    "MIDDLE_EAST": (12.0, 42.0, 25.0, 63.0),
    "RUSSIA_CIS": (45.0, 82.0, 20.0, 180.0),
    "EAST_ASIA": (18.0, 53.0, 73.0, 146.0),
    "EUROPE": (35.0, 72.0, -12.0, 40.0),
    "SOUTH_ASIA": (5.0, 37.0, 60.0, 92.0),
    "AFRICA": (-35.0, 37.0, -18.0, 52.0),
    "AMERICAS": (-56.0, 72.0, -170.0, -30.0),
}


def fetch_states(timeout: int = 25) -> list:
    req = urllib.request.Request(URL, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")).get("states") or []


def shares(states) -> dict:
    """区域内飞机数 / 全球有坐标的飞机数。"""
    pts = []
    for st in states or []:
        if not isinstance(st, (list, tuple)) or len(st) < 7:
            continue
        try:
            lon, lat = st[5], st[6]
        except (IndexError, TypeError):
            continue
        if lon is None or lat is None:
            continue
        pts.append((float(lat), float(lon)))
    if not pts:
        return {}
    total = float(len(pts))
    out = {}
    for region, (la0, la1, lo0, lo1) in REGION_BOXES.items():
        n = sum(1 for la, lo in pts if la0 <= la <= la1 and lo0 <= lo <= lo1)
        out[region] = n / total
    return out


def series_from(shares_map: dict, day: date) -> dict:
    """份额取负、换成百分点写进序列：空域塌陷 → 正 z → 压力上升。

    乘 100 不是为了好看：`robust_scale` 有一个 0.5 的绝对地板，
    0.00x 量级的数会被地板整个吃掉，z 永远贴着 0。
    """
    return {(region, "physical"): {day: -v * 100.0} for region, v in shares_map.items()}


def fetch_day(day: date, timeout: int = 25):
    """返回 (series_fragment, health)。失败就返回空，让分项回到"未接入"。"""
    health = {"source": "opensky", "ok": False, "items": 0, "error": ""}
    try:
        states = fetch_states(timeout=timeout)
    except Exception as exc:                      # noqa: BLE001
        health["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return {}, health
    sh = shares(states)
    if not sh:
        health["error"] = "没有带坐标的飞机"
        return {}, health
    health["ok"] = True
    health["items"] = len(states)
    return series_from(sh, day), health
