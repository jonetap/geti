# GETI · 全球升级张力指数

一个**不预测战争、只测量张力**的仪表盘。零服务器：抓取与计算全部跑在 GitHub Actions，
结果是一个静态页 + 一份 `snapshot.json`。固定成本 ¥0，只在你选配 LLM 抽取时才有 token 花费。

> **GETI 不是开战概率。** 它是 0–100 的信号强度，50 = 与过去 120 天中位数持平。
> 概率需要可校准的基准率，而"国家间新开战"每年个位数——几十条新闻撑不起三位有效数字。
> 做体温计，不做算命先生。

---

## 30 秒看懂它现在能做什么

- 从 **GDELT 每日事件文件**（自带 CAMEO 事件码、Goldstein 分值、提及量）取全球事件流
- 从 **8 个公共 RSS** 取新闻，做 SimHash + 字符 2-gram 去重，**按独立起源计数**（十家转载 = 1 个源）
- 六分项（军事 / 物理征兆 / 外交 / 经济 / 内部动荡 / 叙事）按区域做**稳健 z 分数** → 加权 → `tanh` 映射到 0–100
- 全球指数 = `0.5·最强区域 + 0.5·P90(区域)`（不用均值，防尾部被稀释）
- 输出：指数、24h 变动、置信度、区域分解、分项驱动、事件流、突发（未确认高危）、源健康
- 历史不足 14 天**拒绝给 z 分数**；说不清在哪的事件**不进全球指数**（只展示）

---

## 上线：5 步，约 10 分钟

1. 把 `war-index/` 推到一个 **GitHub 仓库**（公开库 Actions 分钟数无限；私有库 2,000 分钟/月也够）
2. 仓库 → **Settings → Pages → Source** 选 **GitHub Actions**
3. 仓库 → **Actions → geti → Run workflow** 手动跑一次
4. 打开 `https://<用户名>.github.io/<仓库名>/` 看页面
5. 之后每 3 小时自动更新（`17 */3 * * *`，UTC）

**不需要**：服务器、域名、备案、API key、信用卡。抓取在 GitHub runner 上跑，天然境外网络。

### 三个必须知道的坑

| 坑 | 后果 | 已处理 |
|---|---|---|
| 仓库 **60 天无活动**，schedule 被自动停用 | 静默停更，不报错 | workflow 每次提交快照 = 每天至少一次活动 |
| cron 有 5–15 分钟调度延迟 | 时间戳不精确 | 页面显示 `generated_at`，不假装准点 |
| 私有库每月 2,000 分钟 | 每小时跑会卡线 | 3 小时一次 ≈ 480 分钟/月 |

---

## 本地跑

```bash
# 1) 离线自检：合成数据，不联网、不烧 token
python -m unittest discover -s tests          # 18 个测试
python -m pipeline.cli demo --days 120 --out data/snapshot.json --series data/series.jsonl

# 2) 真数据（需要能访问 GDELT / RSS 的网络）
python -m pipeline.cli run --out web/snapshot.json --series data/series.jsonl
python -m pipeline.cli health                 # 看各源存活状态

# 3) 预览（浏览器禁止 file:// 页面 fetch 本地 JSON，必须起静态服务器）
python -m http.server 8080 -d web
```

只依赖 **Python 3.11 标准库**，没有第三方包。

---

## 目录

```
pipeline/
  model.py    数据模型、区域/分项定义、源先验、CAMEO 映射
  parse.py    GDELT 61 列解析、RSS/Atom 解析
  cluster.py  SimHash + 字符 2-gram 相似度、时间窗聚类、起源归并
  score.py    稳健 z、可信度合成、证据量、分项/区域/全球指数、关键词兜底
  build.py    组装 snapshot.json（含归因、突发、置信度）
  fetch.py    网络层（GDELT 日文件、RSS、源健康）
  store.py    series.jsonl 持久化（CI 无状态 → 历史必须落盘）
  synth.py    合成数据（仅用于测试）
  cli.py      命令行
web/
  index.html  单页仪表盘（无依赖，直接 fetch snapshot.json）
data/
  series.jsonl        每 (区域, 分项, 日) 一行，CI 每天提交
  source_health.json  各源 last_success_at / 连续失败次数
.github/workflows/geti.yml
```

---

## 算法（可复核）

```
z̃ = clip(robust_z(x), −3, 3)                 # 中位数 + MAD，非均值/标准差
S_region = 50 + 50·tanh( Σ wᵢ·z̃ᵢ / 1.6 )      # 不做权重归一化：缺项=缺证据，不放大
GETI     = 0.5·max(S_region) + 0.5·P90(S_region)
Δz       = (GETI_t − EWMA_{t−1}) / (1.4826·MAD_{t−1})

w = 军事 .26 / 物理 .22 / 外交 .18 / 经济 .15 / 内部 .11 / 叙事 .08

confidence = (1 − Π(1 − cᵢ·dᵢ)) · (0.70 + 0.30·min(n,3)/3)
evidence   = (severity/5) · √confidence · (0.85 + 0.30·specificity)
```

设计取舍（为什么不用概率、为什么独立起源比源权重更重要、为什么低权重层看密度突变）
见 [`DESIGN.md`](DESIGN.md)。

---

## 当前限制（诚实清单）

- **没有 LLM 抽取**：目前靠 GDELT 事件码 + 关键词表兜底。中文/小语种标题的严重度会低估。
- **区域是靠首都/国家名匹配**的，跨区域事件会落到单一区域。
- **"物理征兆"分项基本是空的**：还没接 NOTAM / 空域关闭 / 航班绕飞。这是设计里权重第二高的分项（.22）。
- **没有回测**：`DESIGN.md` 里列的锚点（2014 克里米亚、2022-02 俄乌等）还没跑。
- **没有推送**：企业微信群机器人 / Telegram 还没接。
- **GDELT 日文件有数小时延迟**，所以"突发"最多滞后半天；15 分钟级增量文件未接入。

## 下一步优先级

1. 接 **NOTAM / 空域关闭**（补上 .22 权重的物理征兆）
2. **回测** 5 个历史锚点，给出"提前了几天 / 误报率"——这是唯一能证明它有用地的东西
3. LLM 只对 **L2 候选（约 120 条/天）** 做结构化抽取，日成本约 ¥1–2
4. 企业微信 webhook 推每日一条摘要
