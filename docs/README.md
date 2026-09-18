# 板块评分工作台

借鉴「帽子哥」投资分析框架（分批-纪律型 + 跷跷板轮动 + 解禁筹码供给 + 日式低增长应对）构建的
**A 股行业板块量化评分与新闻工作台**。

- 覆盖 **申万二级行业 120 个**（可改一行配置切回一级 31 个或切到三级）
- 每个交易日**盘中每 30 分钟**自动评分（09:35–15:05，共 10 次），15:35 收盘复核并归档
- **板块评分视图**：12 维评分 + 宏观分组成决策矩阵，输出可执行动作而非单一信号
- **回撤 / 涨幅视图**：逐板块的 20/60/250 日从高点回撤、从低点反弹、区间位置
- **大盘关键位**：上证 / 沪深300 的**日线**·周线·月线·年线压力位与支撑位（日线档给出「位置区 ×N」）
- **事件日历**：未来 120 天的美联储议息 / 非农 / CPI·PCE / LPR / 政策会议 / 大额解禁，按优先级与类别筛选
- 手机浏览器打开即用，可「添加到主屏幕」当 App 用
- 全免费数据源，零服务器（GitHub Actions + GitHub Pages）

> 评分仅作研究参考，不构成投资建议。详见 [DISCLAIMER.md](DISCLAIMER.md)。

## 文档索引

| 文档 | 用途 | 什么时候看 |
|---|---|---|
| **[炒股工作台_交接文档_v2.2_20260916.md](炒股工作台_交接文档_v2.2_20260916.md)** | **现状与交接书**：实测数据通道、产物 schema、红线约定、运维手册、踩坑清单、待办 | **接手第一份就读它** |
| [部署与投产手册.md](部署与投产手册.md) | 从零把项目上线到 GitHub Pages 的完整步骤 | 要重新部署 / 换仓库时 |
| [炒股工作台_交接文档_v1.0_20260911.md](炒股工作台_交接文档_v1.0_20260911.md) | 设计规格书（需求全景、评分维度设计意图） | 想了解「为什么这么设计」时 |
| [backtest_report_hist.md](backtest_report_hist.md) | **长历史点内重建回测**（2019-07 至今，349 个评价日 × 120 板块）：组合分与**逐条规则**的短/中/长期有效性、解禁事件研究、分年度稳定性 | **想动权重 / 改因子 / 判断某条规则有没有用时** |
| [backtest_report.md](backtest_report.md) | 前瞻监控回测（IC / 分层 / 多空），由 `pipeline/backtest.py` 生成 | 归档快照积累起来之后 |
| [DISCLAIMER.md](DISCLAIMER.md) | 免责声明 | — |

> 站点：**https://chuanwudi46-ops.github.io/Big-A/**　|　手机扫码：`手机访问二维码.png`
> 交接文档的历史版本（v1.0 是规格书，v2.0/v2.1 已被 v2.2 取代）均已删除，以免看错版本。

---

## 一、它是怎么跑的

```
GitHub Actions（免费定时执行，每交易日 12 次）
  09:10  warmup  抓历史K线增量 / 解禁日历 / 新闻池 / 财经日历（当天唯一一次）
  09:35…15:05    每 30 分钟一次 score：抓快照 -> 12维评分 -> 生成 JSON（大盘关键位 +
                 事件日历）-> 提交 -> 重部署 Pages（**盘中不写归档**）
  15:35  close   用收盘价重算，并写**当天唯一一份归档**
        ↓ 产物提交到仓库
GitHub Pages（静态托管）
        ↓ 手机打开
纯前端 PWA：读 JSON 秒开 -> 1 次 JSONP 补实时快照 -> 刷新界面
        ↺ 每 5 分钟静默探一次 meta.json，发现新版只提示「点此刷新」
```

**为什么要"预计算 + 实时补丁"**：Actions 的定时任务本身有 5–30 分钟延迟（整点前后最拥堵），
不能作为盘中决策的唯一时间源。因此评分由 CI **每 30 分钟**预计算（保证维度完整、可留痕），
而当日涨跌由前端 1 个 JSONP 请求补上（保证你此刻看到的就是此刻的市场）。

> **时间语义**：页面上所有时间都是**北京时间**。产物时间戳统一由 `pipeline/clock.py` 生成
> —— CI runner 的系统时区是 **UTC**，直接用 `datetime.now()` 会把北京 15:43 写成 07:43，
> 页面上看起来像「一整天没更新」（详见交接文档 §3.3）。宏观条右侧除绝对时间外还给
> **相对时间**（「3 分钟前」），超过约 26 小时标黄。

## 二、快速开始

### 1. 建仓库并上传

把整个目录推到 GitHub 仓库。

### 2. 开启 Pages

`Settings → Pages → Source` 选择 **GitHub Actions**。

### 3. 配置 tushare token（可选但推荐）

`Settings → Secrets and variables → Actions → New repository secret`

- Name：`TUSHARE_TOKEN`
- Value：你的 tushare token（[tushare.pro](https://tushare.pro) 注册后获取）

> 未配置时不会报错，估值维度会自动降级为「252 日价格分位代理」，并在页面标注口径。

### 4. 首次初始化板块清单

本地（或任意装有 Python 的环境）执行一次：

```bash
pip install -r pipeline/requirements.txt

# 方式一：用现成的白名单（board_universe.json 已随仓库提供，申万二级 120 个）
python pipeline/sync_boards.py     # 生成 pipeline/config/sector_meta.json

# 方式二：重新生成白名单（需要能访问申万/东财）
python pipeline/build_universe.py            # 由申万名单 ∩ 东财板块自动生成
python pipeline/build_universe.py --level 申万一级行业   # 想换层级就改这个参数
python pipeline/sync_boards.py --force       # 换层级后必须重建元数据
```

把生成的 `sector_meta.json` 提交上去。这一步会把实时板块清单与 `keywords_seed.json`
合并，并为缺少关键词的板块补默认值（脚本会提示哪些板块建议补充关键词）。

> `build_universe.py` 还会**按 K 线可用性自动剔除标的**
> （判据：条数 ≥250 **且** 最后一根 bar 在 45 天内），
> 被剔除的会记进 `board_universe.json` 的 `no_kline` 字段，便于事后核对。

### 5. 手动跑一次数据

`Actions → daily-score → Run workflow`

- **第一次：stage 选 `warmup`**（抓 120 个板块的日 K 线与各类中间数据，实测约 7 s）
- **第二次：stage 选 `score`**（评分并产出 `web/data`，实测约 9 s）

> ⚠️ **顺序不能反**。K 线缓存的 key 带 `run_id`，换宇宙后旧缓存里没有新板块，
> 只跑 `score` 会对新板块 `[skip]`，结果 `count` 不足。
> 换宇宙 / 大改之后一律「先 warmup 再 score」。

之后每天会自动运行。

### 6. 手机上使用

打开 `https://<用户名>.github.io/<仓库名>/`

- iOS Safari：分享 → 添加到主屏幕
- Android Chrome：菜单 → 添加到主屏幕

## 三、12 维评分口径

| # | 维度 | 权重 | 说明 |
|---|---|---|---|
| 1 | 动量 | 10% | 5/20/60 日涨幅加权 + 相对沪深300 超额 |
| 2 | 趋势结构 | 10% | MA20/MA60 排列 + KDJ(9,3,3) 金叉与 J 值 |
| 3 | 量价健康 | 8% | 缩量调整加分、上涨放量加分、量价背离扣分 |
| 4 | 换手率位置 | 8% | 250 日分位，**低位加分**（`1 − 分位`）。2026-09-16 修正：原代码是「越高分越高」，与注释/前端/回测三方都相反，见交接文档 6.5.3 |
| 5 | 主力资金流 | 10% | 主力净流入占比 |
| 6 | 估值位置 | 10% | PE 分位；无 token 时用 **252 日价格分位**代理（越高越好）。**注意 `factors.valuation()` 的 docstring 沿用 PE 语义写「越低越好」，与实际代码方向相反 —— 回测支持代码，已修的是注释**，见交接文档 6.5.3 |
| 7 | 周期位置 | 8% | 价格动量 + 涨价信号关键词 |
| 8 | 行业出清度 | 6% | 静态表（人工维护） |
| 9 | 政策强度 | 6% | 反内卷 / 稳增长 / 出海 关键词命中 |
| 10 | 新闻情绪 | 12% | 情绪分 × 时效衰减（半衰期 36h） |
| 11 | 板块共振 | 12% | 成分股涨跌一致性 |
| 12 | 筹码供给 | 罚分 0–20<br>**实际上限 4** | **减持公告**（上限 4 分）。<br>**解禁项已于 2026-09-16 退出罚分**（`release_weight: 12.0 → 0.0`）：四个独立口径的实测一致说明「解禁 ≠ 利空」、方向上甚至相反（被罚得越狠的板块未来反而略好），照原样罚等于扣错人。解禁占比/市值仍写在 `penalty_detail` 里作**提示**，不进分数。见交接文档 6.5.4 |

```
S = clamp(100 × Σ(w·f) − 筹码罚分, 0, 100)
    筹码罚分 = min(20, 0 × 解禁压力归一 + 4 × 减持久归一)   # 解禁项系数 2026-09-16 起为 0
命中负面清单 → 分数上限压至 45 并标「回避」
```

**宏观分 M**（股市上涨四要素）：流动性 30% + 量价 25% + 政策 25% + 情绪 20%，
其中情绪项做**逆向映射**（散户情绪极差 ≈ 反向指标）。

**决策矩阵**：板块分定方向、宏观分定力度。

| S ↓ / M → | M≥70 进攻 | 40≤M<70 中性 | M<40 防守 |
|---|---|---|---|
| ≥75 | 按网格分批建仓 | 小仓试探（≤半仓） | 只观察不动手 |
| 60–75 | 观察，跌破支撑再加 | 观察 | 观察 |
| 30–45 | 逢高减 30% | 逢高减 | 逢高减（加速） |
| <30 | 回避 | 回避 | 回避 |

## 四、自定义

| 想改什么 | 改哪里 |
|---|---|
| 权重、分级阈值、宏观门槛 | `pipeline/config/weights.yaml` |
| **事件日历的事件与优先级** | `pipeline/config/events_rules.json`（规则按顺序匹配，第一条命中即用；`curated` 存惯例政策会议） |
| 负面清单（可手动增删） | `pipeline/config/keywords_seed.json` 的 `blacklist`，改完重跑 `sync_boards.py` |
| 板块关键词 / 出清阶段 | `pipeline/config/keywords_seed.json` |
| 评分时点 / 刷新频率 | `.github/workflows/daily.yml` 的 cron（**UTC** 时间，北京时间 = UTC+8）。现为 **12 次/日**：09:10 预热 + 09:35–15:05 每 30 分钟 score + 15:35 close。分钟取 `:05`/`:35` 以避开 GitHub 的整点拥堵 |
| 归档时机 | `weights.yaml` 的 `archive.mode`（默认 **`on_close`**：只有 close 阶段写归档；设 `always` 则每次 score 都写，仓容约 640 MB/年） |
| 自选上限 | `web/app.js` 的 `MAX_SEL`（当前 12） |

## 五、本地调试

```bash
# 仅验证因子与评分逻辑（不联网）
python -c "import sys; sys.path.insert(0,'pipeline'); import factors, score; print('ok')"

# 生成合成数据，预览界面（⚠️ 假数据，仅用于调 UI）
python pipeline/make_demo.py

# 额外生成 60 天仿真K线 + 历史评分快照，用于离线验证回测全链路
python pipeline/make_demo.py --simulate 60 --rebuild-map
python pipeline/backtest.py            # 输出 docs/backtest_report.md

# 真实运行
python pipeline/main.py --stage warmup
python pipeline/main.py --stage score
python pipeline/main.py --stage score --force     # 忽略交易日判断
python pipeline/main.py --stage warmup --rebuild-map   # 强制重建个股-板块映射

# 事件日历：改完 events_rules.json 直接跑它看结果（会回源拉日历并打印全部事件）
python pipeline/events.py

# 大盘关键位（日/周/月/年四档）
python pipeline/levels.py

# 本地预览前端
cd web && python -m http.server 8080
# 浏览器打开 http://127.0.0.1:8080/  （手机同 Wi-Fi 可用 http://<电脑IP>:8080/）
```

## 六、回测与权重校准

这里有**两条互补的路**，别混用：

| | 前瞻监控 | 长历史重建 |
|---|---|---|
| 脚本 | `pipeline/backtest.py` | `pipeline/backtest_hist.py` |
| 样本 | `web/data/archive/` 每日归档的**真实发布分数** | 真实日 K 线，**逐日重建**当时该有的因子值 |
| 覆盖维度 | 12 维全部 | **只有 6 个技术面维度**可回溯（其余需当期快照） |
| 现状 | ⏳ 归档历史**全是同一天**的多份快照，按日期去重后只剩 1 天 → 「样本不足」曾是**结构性**的。<br>**已修机制**：每次 score 另写轻量 summary，跨日样本从 2026-09-16 起开始积累（仍受 `min-boards`/天数限制） | ✅ 2019-07 至今 349 个评价日 × 120 板块 = **41,762 行** |

### 长历史重建（推荐先用这个）

```bash
# ① 一次性取数（约 15 s）：120 板块 × 2001 根日线 + 2019 至今 1.7 万条解禁明细
python pipeline/backtest_hist.py --fetch

# ② 跑回测（首次实测约 8 分钟：3 分钟建面板 + 5 分钟检验与出报告）
python pipeline/backtest_hist.py --horizons 5,20,60,120 --stride 5

# ③ 只改报告措辞时复用已建好的面板，跳过重建（约 20 s）
python pipeline/backtest_hist.py --reuse-panel
```

产出 `docs/backtest_report_hist.md`，含：组合分短/中/长期 IC、逐因子有效性、
**逐条规则检验**（分级阈值 / 量价健康 / 趋势结构及其 KDJ 拆解 / 换手率 / 估值方向 /
动量反转 / 解禁规则的事件研究 + **解禁供给压力的生产同款口径检验** / 宏观门）、
分年度稳定性、偏差声明与复现命令。

要点：

- 评价日只喂 `df[:t+1]` 给 `factors.py` 的**原函数**，无前视、不另写一套公式
- 分档表一律用**同日超额收益**（减掉当天全市场等权）。用绝对收益会把市场 beta 混进来，
  实测能让同一条规则得出**相反**结论
- 数据落在 `bt_data/`（已在 `.gitignore`，且**必须在 `web/` 之外**，否则会被 Pages 发布）
- ⚠️ **改过 `factors.py` / 进面板的权重之后不要直接 `--reuse-panel`**：
  面板里存的是**算好的因子值**，复用旧面板等于拿旧公式的结论评价新公式，而且不会报错。
  面板 meta 里记了 `factor_rev` 指纹，对不上时脚本会**直接拒绝**；要强行复用得加
  `--allow-stale-panel`。报告头部也打印该指纹，**跨版本结论不可直接对比**。
  指纹口径已收窄（2026-09-16）：只覆盖 `factors.py` + `PANEL_SCHEMA` + `weights.yaml` 里
  **真正进面板的** `window` 与 6 个可回溯权重 —— 改 `penalty` / `macro` / `archive` 这类
  与面板无关的键不会再误触发重建。详见交接文档 §11.2。
- §1 的保真度校验是拿「某个归档快照」当对照物的。归档是**某一版因子代码**的产物，
  所以改了因子方向之后，该项因子的重建值与归档会变成**负相关**，报告会自动提示
  「对照物过期」而不是让你以为重建坏了。CI 跑出新归档后即恢复。
- 报告里出现的 `turnover` 分档表按**原始分位**（面板列 `turn_pct`）切，
  与因子方向无关 —— 所以「最低20% / 最高20%」的标签在任何一版代码下都成立。

### 前瞻监控（跨日样本攒起来之后）

```bash
python pipeline/backtest.py                          # 默认 1/5/10/20 日
python pipeline/backtest.py --horizons 3,10,30 --quantiles 5
```

输出 `docs/backtest_report.md` 与 `web/data/backtest_detail.csv`，包含：

| 指标 | 含义 | 判读 |
|---|---|---|
| IC 均值 | 评分与未来收益的 Spearman 秩相关 | >0.03 可用，>0.06 优秀 |
| ICIR | IC 均值 / IC 标准差 | >0.3 稳定，>0.5 优秀 |
| 分层收益 | 按评分分 5 档的平均收益 | 应单调递增 |
| 多空胜率 | Top 档 − Bottom 档 为正的频率 | >55% |
| 多空累计 | Top − Bottom 累计收益 | — |
| **逐因子 IC** | 每个维度各自与未来收益的秩相关（需 summary 里带因子） | 见报告第三节 |

> **它吃的是「跨日」样本，不是「文件数」**。归档**只在 15:35 的 close 阶段写**
> （`archive.mode: on_close`），每天两份：整份 `YYYYMMDD_HHMM.json`（~200 KB）与轻量
> `YYYYMMDD_HHMM_summary.json`（~33 KB，含 11 维因子值）。
> 盘中 score 不再写归档 —— 整份快照 200 KB × 12 次/日 ≈ 640 MB/年，
> 而同日的第二份在按日期去重后对 IC **零增量**。**同一天无论写几份都只算 1 天**，
> 所以样本要**按天**积累。攒够之后，这条路能评估**线上真实发布分**（12 维全套），
> 包括长历史重建覆盖不到的 5 维（主力资金流 / 出清度 / 政策 / 新闻情绪 / 板块共振）。
> 样本不足时脚本不报错，而是输出说明性报告 + 判读标准。
> 用 `--simulate` 生成的合成数据可立刻验证脚本本身是否正常。

## 七、关于数据源的可用性（重要，已按实测校准）

**一句话**：东财的 **K 线通道在境外（GitHub Actions）完全不可用**，因此 K 线已改走**腾讯行情**；
东财只保留它可用的部分（`push2delay` 的 clist、`datacenter-web`）。

各用途的取数优先级：

| 用途 | 优先级 | 说明 |
|---|---|---|
| 板块日 K 线 | **腾讯 → 东财直连 → akshare** | 腾讯的 `newfqkline/get` 是唯一可用通道 |
| 板块快照 / 成分股 | 东财 `push2delay` clist 直连 | `push2` 在境内外都不可用 |
| 高管持股变动 | `datacenter-web` + 服务端 `filter` | 直连 0.98 s；akshare 同数据要 8 分 49 秒 |
| **财经日历（事件日历）** | `datacenter-web` 的 **`RPT_CPH_FECALENDAR`** 报表 | 4 个月区间 1543 条 / 1.29 s；报表名从 `data.eastmoney.com/cjrl` 的页面 JS 里挖出来的 |

实测可达性：

| 端点 | 境外（CI） | 国内本机 |
|---|---|---|
| `push2.eastmoney.com` | ❌ 302→502 | ❌ 不可达 |
| `push2his.eastmoney.com`（K线） | ⚠️ 连几次后断连 | ❌ 断连 |
| `push2delay` **clist** | ✅ | ✅ |
| `push2delay` **K线** | ⚠️ 软限流（HTTP 200 但数据为空） | ⚠️ 同左 |
| `datacenter-web` | ✅ | ✅ |
| **腾讯 / 新浪行情** | ✅ | ✅ |

> ⚠️ **不要以为「CI 在境外所以东财更好访问」** —— 恰恰相反，东财对境外出口有地区封锁。
> 遇到抓不到数据，先按上表确认走的是哪条通道，而不是换网络重试。
> 完整的探针结论与排查方法见交接文档 v2.1 第 4 节。

如果所有通道都失败：

1. 确认 `pipeline/sources.py` 里的优先级没被改错（K 线必须是腾讯优先）
2. 用 `python pipeline/make_demo.py --simulate 60` 先生成合成数据把界面调通
   （产物带 `demo: true`，前端会显示警示横幅，不会与真实评分混淆）

## 八、已知待补项

| 项 | 说明 |
|---|---|
| ~~换手率因子方向写反~~ ✅ **已修**（2026-09-16） | 长历史回测（349 评价日 × 120 板块）裁定：低换手明显更好（最高 20% 比最低 20% 长期差 **-1.09pt**，5 年符号 5/5 全负），原代码却是 `_pctrank(turnover)`。已改为 `1 - _pctrank(turnover)`，组合分 IC 从 -0.016/-0.031/-0.038/+0.018 改善到 **-0.001/-0.014/-0.021/+0.036**；`turnover` 中期 IC 由 -0.076 翻正到 **+0.083** |
| ~~解禁罚分方向~~ ✅ **已修**（2026-09-16） | 补了生产同款口径的决定性检验（四个独立口径一致：被罚得越狠 → 未来**略好**），故 `release_weight: 12.0 → 0.0`（退出罚分，降级为提示项，**不反向加分**）。见交接文档 6.5.4 |
| **权重为先验值** | 12 维权重尚未用真实历史数据校准。⚠️ 原计划「攒 2–4 周快照后跑 `backtest.py`」**方向是错的**：归档曾全是同一天的多份快照，按日期去重后样本数恒为 1。<br>**已解决一半**：`main.py` 在 **close 阶段**另写一份**分数矩阵 summary**（`archive.summary`）供跨日积累（盘中不写，见第六节）；`backtest.py` 也已能消费它并输出**逐因子 IC**。剩下的只是**等天数**。<br>同时**长历史重建法**（见第六节）已可用，但它只能覆盖 6 个可回溯维度 |
| 行业估值无真实 PE | 估值维度（权重 10%）现用 252 日价格分位**代理**，产物标 `valuation_source`。<br>**不是映射问题**：申万↔东财映射已建好（`board_universe.json` 带 `sw_codes` / `board_codes`）。<br>真障碍是 **tushare 拿不到行业估值** —— `index_dailybasic` 需 4000 积分起，且官方只提供上证综指/深证成指/上证50/中证500/中小板指/创业板指，**不含任何行业**。<br>下一步走 akshare 免费行业市盈率（巨潮 / 中证） |
| 动量维度证据冲突 | `momentum` 中期 IC 为负（-0.043）但逐日配对为正（+0.98pt）—— 两个口径打架，说明 5/20/60 日涨幅加权内部有项在反向拖累，须按 `trend` 的 KDJ 拆解那份做法逐子项拆开 |
| 尾盘/盘中差异 | 盘中各次评分基于未完成 bar（产物标 `intraday: true`；15:35 收盘复核后自动转 `false`），收盘前后数值会变化 |
| 板块成分股为快照 | `stock_board_map` 缓存 7 天，成分股调整期内可能短暂不准 |
| 真机未实测 | 本机无法模拟手机网络，需在 iOS Safari / Android Chrome 各验一次 |

> 以下已于 P1 补齐：宏观四要素真实数据（`macro.py`）、解禁精确归属与减持计数（`chips.py`）、
> 真实交易日历、评分回测与 IC 分析（`backtest.py`）、前端 IndexedDB 缓存。

## 九、数据来源

| 数据 | 实际来源（**按实测校准，不是设计意图**） |
|---|---|
| 板块历史 K 线 | **腾讯** `proxy.finance.qq.com/ifzqgtimg/...newfqkline/get`（`pt01`+申万码）<br>❌ 东财 K 线在境外/本机都不可用；akshare 仅作兜底 |
| 板块快照 / 成分股 | 东方财富 **`push2delay`** clist 直连（`push2` 不可达）<br>前端实时补丁域名顺序 `push2` → `push2delay`，**不能反** |
| 行业资金流 | 东财 clist 的 `f62`/`f184` 字段（随板块快照同一次请求） |
| 新闻 | akshare（财联社电报 / 东财 7×24） |
| 限售解禁 | akshare `stock_restricted_release_detail_em`（回测用历史明细：**逐月分片**抓取，`bt_data/release_hist.parquet`）<br>⚠️ 该接口单季**恰好 400 行**封顶，必须分片 |
| 高管持股变动 | 东财 `datacenter-web` + 服务端 `filter`（直连 0.98 s；akshare 同数据 8 分 49 秒） |
| **财经日历（事件日历）** | **东方财富 `RPT_CPH_FECALENDAR` 报表**（真实事件表）<br>+ 规则推算（股指期货交割日）+ `release.parquet`（解禁）+ 人工惯例清单 |
| 行业估值 | ❌ **当前无真实行业估值**，用 252 日价格分位代理（产物标 `valuation_source`）<br>tushare `index_dailybasic` 需 4000 积分且**不含任何行业**，此路已确认走不通 |
| 盘中实时快照（前端） | 东方财富 `push2.eastmoney.com` JSONP 直连（失败自动回退 `push2delay`） |

所有接口均为公开免费通道，可用性不保证；仅用于个人研究。
