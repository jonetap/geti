"""命令行入口。

    python -m pipeline.cli demo                      # 离线合成数据
    python -m pipeline.cli run                       # 拉真实数据
    python -m pipeline.cli backfill --days 120       # 回补历史（本地跑）
    python -m pipeline.cli health                    # 各源健康状态
    python -m pipeline.cli serve                     # 本地预览 web/
"""
from __future__ import annotations

import argparse
import functools
import http.server
import os
import random
import socketserver
import sys
from datetime import date, datetime, timedelta, timezone

from . import backtest as bt
from . import build, cluster, composite, epidemic, hazard, market, store
from . import fetch as fetcher
from .synth import synth_items, synth_signals

UTC = timezone.utc
DEFAULT_SERIES = "data/series.jsonl"
DEFAULT_HEALTH = "data/source_health.json"
DEFAULT_AUX = "data/aux_series.jsonl"
MIN_EVENTS_PER_DAY = 200


def _span(target: date, n: int) -> list[date]:
    return [target - timedelta(days=i) for i in range(n - 1, -1, -1)]


def _assemble(signals, items, target, now, coverage, series_path, span, extra=None):
    clusters = cluster.merge_by_origin(cluster.cluster_items(items))
    scored = build.score_clusters(clusters, now=now)

    raw = build.merge_series(
        build.pressure_from_signals(signals),
        build.pressure_from_clusters(scored),
    )
    merged = store.save_series(series_path, raw, keep_days=span + 40, end=target)

    coverage = dict(coverage)
    coverage["clusters"] = len(clusters)
    coverage["origins"] = sum(c.n_origin for c in clusters)

    snap = build.build_snapshot(merged, target, _span(target, span), scored, coverage, now=now)
    if extra:
        snap.update(extra)
    return snap


def _aux_block(day, items, end_is_known: bool = True) -> tuple[dict, list[dict]]:
    """灾害层 + 疫情层。

    **任何一侧失败都不许影响主指数**：这里全部包在 try 里，抓不到就把分项留空，
    页面显示"数据累积中"。宁可少一个数字，也不要给一个假的 50。
    """
    hz: dict = {}
    ep: dict = {}
    health: list[dict] = []
    try:
        hz, h1 = hazard.fetch_day(day)
        health += h1
    except Exception as exc:                            # noqa: BLE001
        health.append({"source": "hazard", "ok": False, "items": 0,
                       "error": f"{type(exc).__name__}: {exc}"[:200]})
    try:
        ep, h2 = epidemic.fetch_day(day, items)
        health += h2
    except Exception as exc:                            # noqa: BLE001
        health.append({"source": "epidemic", "ok": False, "items": 0,
                       "error": f"{type(exc).__name__}: {exc}"[:200]})

    if hz:
        store.save_series(DEFAULT_AUX, composite.readings_to_series("hazard", day, hz),
                          keep_days=400, end=day)
    if ep:
        store.save_series(DEFAULT_AUX, composite.readings_to_series("epidemic", day, ep),
                          keep_days=400, end=day)

    series = store.load_series(DEFAULT_AUX)
    return {
        "hazard": composite.subindex(series, "hazard", day),
        "epidemic": composite.subindex(series, "epidemic", day),
    }, health


# ---------------------------------------------------------------- 子命令


def cmd_demo(args: argparse.Namespace) -> int:
    end = date.fromisoformat(args.date) if args.date else date(2026, 11, 3)
    now = datetime(end.year, end.month, end.day, 12, 0, tzinfo=UTC)
    signals = synth_signals(days=args.days, end=end, spike_day=args.spike)
    items = synth_items(days=2, end=now)
    coverage = {
        "sources_ok": 16,
        "sources_failed": 2,
        "items": len(signals) + len(items),
        "mode": "synthetic",
    }
    snap = _assemble(signals, items, end, now, coverage, args.series, args.days)

    # 演示用的灾害/疫情：编一组平稳的历史，让页面三个模块都能渲染
    rnd = random.Random(20261103)
    fake: dict = {}
    for kind, keys in (("hazard", hazard.WEIGHTS), ("epidemic", epidemic.WEIGHTS)):
        for k in keys:
            cell = {end - timedelta(days=i): round(10 + rnd.random() * 6, 2)
                    for i in range(60)}
            cell[end] = round(15.5 + rnd.random() * 3, 2)
            fake[(kind, k)] = cell
    snap["hazard"] = composite.subindex(fake, "hazard", end)
    snap["epidemic"] = composite.subindex(fake, "epidemic", end)
    snap["doom"] = composite.doom(snap.get("geti"), snap["hazard"]["index"],
                            snap["epidemic"]["index"])

    store.write_json(args.out, snap)
    print(f"[demo] {args.out}  GETI={snap['geti']}  Δz={snap['delta_z']}  "
          f"驱动={snap['driver_label']}  置信度={snap['confidence']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)

    signals, gh, resolved = fetcher.fetch_gdelt_daily(
        days=args.days,
        max_lookback=args.lookback,
        min_mentions=args.min_mentions,
    )
    items, fh = fetcher.fetch_feeds()

    mkt: dict = {"mode": "skipped"}
    if not args.no_market:
        try:
            mkt = market.fetch_market()
        except Exception as exc:                        # 市场层永不阻断主流程
            mkt = {"mode": "failed", "error": f"{type(exc).__name__}: {exc}"[:200]}
        health = mkt.get("health") or []
        fh = fh + health

    if resolved is None:
        print("[run] GDELT 连续多日拉不到任何日文件——不写快照（否则会用一个空窗口算指数）",
              file=sys.stderr)
        return 2

    ok = sum(1 for r in gh if r.get("ok")) + sum(1 for r in fh if r.get("ok"))
    bad = len(gh) + len(fh) - ok
    coverage = {
        "sources_ok": ok,
        "sources_failed": bad,
        "items": len(signals) + len(items),
        "gdelt_events": len(signals),
        "mode": "live",
        "data_through": resolved.isoformat(),   # 数据实际截止到哪天
    }

    snap = _assemble(signals, items, resolved, now, coverage, args.series, args.span,
                     extra={"market": mkt})

    aux_block, aux_health = _aux_block(resolved, items)
    snap["hazard"] = aux_block["hazard"]
    snap["epidemic"] = aux_block["epidemic"]
    snap["doom"] = composite.doom(snap.get("geti"),
                            aux_block["hazard"].get("index"),
                            aux_block["epidemic"].get("index"))
    snap["adapters"] = gh + fh + aux_health
    store.write_json(args.out, snap)

    os.makedirs(os.path.dirname(DEFAULT_HEALTH) or ".", exist_ok=True)
    fetcher.save_health(DEFAULT_HEALTH, gh + fh, now=now)

    print(f"[run] {args.out}  数据截止={resolved}  GETI={snap['geti']}  "
          f"末日={snap['doom']['index']}  灾害={snap['hazard']['index']}  "
          f"疫情={snap['epidemic']['index']}  Δz={snap['delta_z']}  "
          f"事件={len(signals)}  新闻={len(items)}  市场={mkt.get('index')}  "
          f"源 ok={ok} bad={bad}")
    if ok == 0:
        print("[run] 全部源失败——本次快照不可用", file=sys.stderr)
        return 2
    return 0


def cmd_backfill(args: argparse.Namespace) -> int:
    """回补历史 GDELT 日文件，把 data/series.jsonl 填满。

    这是**本地跑一次**的命令：每天一个 ~10MB 的 zip，120 天约 1.2GB 流量。
    放 CI 上会烧掉全部免费额度，所以它不在 workflow 里。
    """
    end = (date.fromisoformat(args.end) if args.end
           else datetime.now(UTC).date() - timedelta(days=1))
    start = (date.fromisoformat(args.start) if args.start
             else end - timedelta(days=args.days))

    d = start
    days_ok = 0
    days_bad = 0
    events = 0
    while d <= end:
        sig, _health, res = fetcher.fetch_gdelt_daily(days=1, end=d, max_lookback=0,
                                                      min_mentions=args.min_mentions)
        if res is not None:
            # 护栏：一个 ~10MB 日文件不可能只解析出几十条。真出现了，说明是
            # 解析器/A 列位出了问题——**宁可留空，也不要把这种值写进历史**。
            # 历史一旦被污染，后面所有 z 分数、所有回测都建立在错的基线上，
            # 而且很难再看出来（我们就吃过一次：第一次回补用的是列位错位前的解析器）。
            if len(sig) < MIN_EVENTS_PER_DAY:
                days_bad += 1
                print(f"  {d}  可疑  仅 {len(sig)} 条（< {MIN_EVENTS_PER_DAY}），"
                      f"跳过写入——历史不能带毒", flush=True)
            else:
                raw = build.pressure_from_signals(sig)
                store.save_series(args.series, raw, keep_days=400, end=d)
                days_ok += 1
                events += len(sig)
                print(f"  {d}  ok  {len(sig):6d} 条  累计 {days_ok} 天 / {events} 条",
                      flush=True)
        else:
            print(f"  {d}  missing", flush=True)
        d += timedelta(days=1)

    print(f"[backfill] 完成 {days_ok} 天，{events} 条事件 → {args.series}")
    if days_bad:
        print(f"[backfill] 有 {days_bad} 天因条数异常被判可疑并跳过——"
              f"先查解析器，别急着调指数", file=sys.stderr)
    return 0 if days_ok else 2


def cmd_backtest(args: argparse.Namespace) -> int:
    series = store.load_series(args.series)
    rows = bt.run_all(series)
    summary = bt.summarize(rows)
    for r in rows:
        if r.get("status") != "ok":
            print(f"  {r['name']:16s} 数据不足 —— 跳过")
            continue
        print(f"  {r['name']:16s} 提前={r['lead_days']:2d}天  "
              f"事前峰值={r['peak_pre']:5.1f}（{r['peak_pre_day']}）  "
              f"事后峰值={r['peak_post']:5.1f}  平静期误报={r['fp_rate']:.0%}")
    print(f"[backtest] {summary['verdict']}")
    if args.json:
        store.write_json(args.json, summary)
        print(f"[backtest] 明细 → {args.json}")
    return 0 if summary.get("status") == "ok" else 3


def cmd_health(args: argparse.Namespace) -> int:
    state = fetcher.load_health(DEFAULT_HEALTH)
    if not state:
        print("没有健康记录，先跑一次 run")
        return 0
    for sid, rec in sorted(state.items()):
        flag = "OK " if not rec.get("last_error") else "ERR"
        print(f"{flag} {sid:34s} last_ok={rec.get('last_success_at')} "
              f"fails={rec.get('consecutive_failures', 0)} {rec.get('last_error', '')[:60]}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=args.dir)
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer((args.host, args.port), handler) as httpd:
        print(f"http://{args.host}:{args.port}/  （Ctrl+C 结束）")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nbye")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pipeline", description="GETI 全球升级张力指数")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="用合成数据跑一遍（离线）")
    d.add_argument("--days", type=int, default=120)
    d.add_argument("--date", default="")
    d.add_argument("--spike", type=int, default=None, help="在第 N 天前制造一次尖峰")
    d.add_argument("--out", default="data/snapshot.json")
    d.add_argument("--series", default=DEFAULT_SERIES)
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="拉真实数据并生成快照")
    r.add_argument("--days", type=int, default=2, help="GDELT 日文件取几天")
    r.add_argument("--lookback", type=int, default=6, help="当天文件没发布时往前找几天")
    r.add_argument("--min-mentions", type=int, default=2, help="低于此提及数的丢弃")
    r.add_argument("--span", type=int, default=120, help="指数计算窗口")
    r.add_argument("--out", default="web/snapshot.json")
    r.add_argument("--series", default=DEFAULT_SERIES)
    r.add_argument("--no-market", action="store_true", help="跳过 FRED 市场层")
    r.set_defaults(func=cmd_run)

    b = sub.add_parser("backfill", help="回补历史日文件（本地跑，别放 CI）")
    b.add_argument("--days", type=int, default=120)
    b.add_argument("--start", default="")
    b.add_argument("--end", default="")
    b.add_argument("--min-mentions", type=int, default=2)
    b.add_argument("--series", default=DEFAULT_SERIES)
    b.set_defaults(func=cmd_backfill)

    h = sub.add_parser("health", help="查看各源健康状态")
    h.set_defaults(func=cmd_health)

    t = sub.add_parser("backtest", help="对历史锚点跑回测（需要先 backfill）")
    t.add_argument("--series", default=DEFAULT_SERIES)
    t.add_argument("--json", default="data/backtest.json")
    t.set_defaults(func=cmd_backtest)

    s = sub.add_parser("serve", help="本地起一个静态服务器预览 web/")
    s.add_argument("--dir", default="web")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8080)
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
