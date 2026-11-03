# 你需要做的事（约 10 分钟）

代码已经全部写好并跑通（31 个测试通过，含一次不联网的完整 `run`）。
剩下只有**我做不到的四件事**：注册账号、点授权、按按钮。

---

## 1. 在 GitHub 上建一个**公开**仓库

名字随意，比如 `geti`。

> **为什么必须公开**：公开仓库的 Actions 分钟数无限，私有仓库每月只有
> 2,000 分钟——每 3 小时跑一次约 480 分钟/月，够是够，但没有余量。
> 公开的代价是别人能看到代码，而代码本来也没什么可藏的。

## 2. 把 `war-index/` 里的东西推到仓库根目录

注意是**里面的内容**推到根目录，不是推 `war-index` 这一层：

```
你的仓库/
├── .github/workflows/geti.yml
├── pipeline/
├── tests/
├── web/
├── data/
├── README.md
├── DESIGN.md
└── SETUP.md
```

命令行（在你自己的机器上，`war-index` 目录里执行）：

```bash
git init
git add -A
git commit -m "init: GETI"
git branch -M main
git remote add origin https://github.com/<你的用户名>/geti.git
git push -u origin main
```

## 3. 打开 Pages：Settings → Pages → Source 选 **GitHub Actions**

不要选 "Deploy from a branch"。这个项目的部署产物由 workflow 自己上传。

## 4. 手动跑第一次：Actions → geti → Run workflow

**这一步必须填 `backfill_days = 120`。**

理由：指数用的是「跟过去 90 天比」的 z 分数，至少要有 14 天历史才出数。
不回补的话，前两周页面会一直钉在 50 分，你会以为它坏了。
回补 120 天约下载 1.2GB、跑几分钟，是一次性动作。

之后所有的定时触发都留空，不用再填。

## 5. 等 3–5 分钟，打开 Pages 给你的地址

`https://<你的用户名>.github.io/geti/`

顶部如果显示黄色「示例数据」，说明还在等第一次真实快照；正常应该显示
「数据截止 YYYY-MM-DD」。

---

## 之后会自动发生什么

- 每 3 小时跑一次：抓 GDELT 日文件 + 8 个 RSS + FRED 市场数据 → 重算 → 提交 → 重新部署。
- 每次提交会让仓库保持「活跃」，避免 GitHub 因 60 天无活动**静默停用**定时任务
  （它不会发任何通知，这是最阴的坑）。
- 你不需要做任何事。想改频率就改 `geti.yml` 里的 cron。

## 想本地看一眼（可选）

需要 Python 3.11+，不需要装任何第三方包：

```bash
python -m pipeline.cli demo          # 用合成数据生成示例快照
python -m pipeline.cli serve         # 起 http://127.0.0.1:8080
```

`demo` 不联网，`serve` 只是静态文件服务器。**直接双击 `web/index.html` 是打不开的**——
浏览器禁止 `file://` 页面 fetch 本地 JSON，这是安全策略不是 bug。

---

## 如果出问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 页面一直显示「示例数据」 | 第一次 run 还没成功 | Actions 里看日志；多半是 `run` 返回 2（GDELT 拉不到日文件），重跑一次即可 |
| GETI 一直 50.0 | 历史不足 14 天 | 按第 4 步补 `backfill_days=120` |
| 页面 404 | Pages Source 没选 GitHub Actions，或部署 job 失败 | Settings → Pages 重选一次 |
| 定时任务停了 | 仓库 60 天无活动被停用 | Actions 页面会提示，点 Enable；本项目每 3 小时提交一次，正常不会发生 |
| 某个源一直是 ERR | 抓取器腐烂（站点改版） | `python -m pipeline.cli health` 看 `last_error`，改 `pipeline/fetch.py` 里对应 URL |

## 可选：推到微信

企业微信群机器人 webhook 是免费的，境内可达、零安装。你建好群机器人把
webhook 地址给我，我加一个 `notify` 子命令每天推一条摘要长图/文字。

---

## 现在的能力边界（别期待它做不到的事）

- ✅ 全球升级张力指数、区域分解、六分项驱动、事件流、突发、市场压力面板
- ✅ 全自动、零服务器、零 API key、零月费
- ⚠️ **还没有 LLM 抽取**：目前靠 CAMEO 编码 + 关键词规则，读不懂长文语义
- ⚠️ **物理征兆层基本是空的**（NOTAM/空域关闭/航班绕飞没接）
- ⚠️ **回测没跑过**：`backtest` 命令写好了，但要先有历史数据
- ❌ 没有推送、没有 15 分钟级增量

这些都在 `DESIGN.md` 里列成了后续阶段。先把管线跑通，再往里加东西。
