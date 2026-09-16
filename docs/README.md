# 板块评分工作台

借鉴「帽子哥」投资分析框架（分批-纪律型 + 跷跷板轮动 + 解禁筹码供给 + 日式低增长应对）构建的
**A 股行业板块量化评分与新闻工作台**。

- 覆盖 **申万二级行业 120 个**（可改一行配置切回一级 31 个或切到三级）
- 每个交易日 **14:00** 自动评分（盘中快照），15:30 收盘复核归档
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
| **[炒股工作台_交接文档_v2.1_20260916.md](炒股工作台_交接文档_v2.1_20260916.md)** | **现状与交接书**：实测数据通道、产物 schema、红线约定、运维手册、踩坑清单、待办 | **接手第一份就读它** |
| [部署与投产手册.md](部署与投产手册.md) | 从零把项目上线到 GitHub Pages 的完整步骤 | 要重新部署 / 换仓库时 |
| [炒股工作台_交接文档_v1.0_20260911.md](炒股工作台_交接文档_v1.0_20260911.md) | 设计规格书（需求全景、评分维度设计意图） | 想了解「为什么这么设计」时 |
| [backtest_report.md](backtest_report.md) | 回测报告（IC / 分层 / 多空），由 `pipeline/backtest.py` 生成 | 做权重校准时 |
| [DISCLAIMER.md](DISCLAIMER.md) | 免责声明 | — |

> 站点：**https://chuanwudi46-ops.github.io/Big-A/**　|　手机扫码：`手机访问二维码.png`
> 交接文档 v2.0 已被 v2.1 完全取代（只多了「日线关键位」与「事件日历」两块），已删除以免看错版本。

---

## 一、它是怎么跑的

```
GitHub Actions（免费定时执行）
  13:40  warmup  抓历史K线增量 / 解禁日历 / 新闻池 / 财经日历
  14:00  score   抓盘中快照 -> 12维评分 -> 生成 JSON（大盘关键位 + 事件日历）
  15:30  close   用收盘价重算并归档
        ↓ 产物提交到仓库
GitHub Pages（静态托管）
        ↓ 手机打开
纯前端 PWA：读 JSON 秒开 -> 1 次 JSONP 补实时快照 -> 刷新界面
```

**为什么要"预计算 + 实时补丁"**：14:00 属盘中，GitHub Actions 的定时任务有 5–30 分钟延迟，
不能作为盘中决策的唯一时间源。因此评分由 CI 预计算（保证维度完整、可留痕），
而当日涨跌由前端 1 个 JSONP 请求补上（保证你此刻看到的就是此刻的市场）。

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
| 4 | 换手率位置 | 8% | 250 日分位，低位加分 |
| 5 | 主力资金流 | 10% | 主力净流入占比 |
| 6 | 估值位置 | 10% | PE 分位；无 token 时用价格分位代理 |
| 7 | 周期位置 | 8% | 价格动量 + 涨价信号关键词 |
| 8 | 行业出清度 | 6% | 静态表（人工维护） |
| 9 | 政策强度 | 6% | 反内卷 / 稳增长 / 出海 关键词命中 |
| 10 | 新闻情绪 | 12% | 情绪分 × 时效衰减（半衰期 36h） |
| 11 | 板块共振 | 12% | 成分股涨跌一致性 |
| 12 | 筹码供给 | 罚分 0–20 | 解禁压力 + 减持公告 |

```
S = clamp(100 × Σ(w·f) − 筹码罚分, 0, 100)
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
| 评分时点 | `.github/workflows/daily.yml` 的 cron（UTC 时间，北京时间 = UTC+8） |
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

评分快照会随每日运行自动归档到 `web/data/archive/YYYY/MM/`，积累一段时间后可回测：

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

> **需要 ≥ 2 个交易日快照**才有意义。定时任务会自动累积，因此建议跑 2–4 周后再执行。
> 样本不足时脚本不会报错，而是输出说明性报告。
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
| 权重为**先验值** | 尚未用真实历史数据校准，需积累快照后用 `backtest.py` 调优（建议 2–4 周） |
| 行业估值无真实 PE | 估值维度（权重 10%）现用 252 日价格分位**代理**，产物标 `valuation_source`。<br>**不是映射问题**：申万↔东财映射已建好（`board_universe.json` 带 `sw_codes` / `board_codes`）。<br>真障碍是 **tushare 拿不到行业估值** —— `index_dailybasic` 需 4000 积分起，且官方只提供上证综指/深证成指/上证50/中证500/中小板指/创业板指，**不含任何行业**。<br>下一步走 akshare 免费行业市盈率（巨潮 / 中证） |
| 尾盘/盘中差异 | 14:00 评分基于未完成 bar（产物标 `intraday: true`），收盘后数值会变化 |
| 板块成分股为快照 | `stock_board_map` 缓存 7 天，成分股调整期内可能短暂不准 |
| 真机未实测 | 本机无法模拟手机网络，需在 iOS Safari / Android Chrome 各验一次 |

> 以下已于 P1 补齐：宏观四要素真实数据（`macro.py`）、解禁精确归属与减持计数（`chips.py`）、
> 真实交易日历、评分回测与 IC 分析（`backtest.py`）、前端 IndexedDB 缓存。

## 九、数据来源

| 数据 | 来源 |
|---|---|
| 板块行情 / 历史K线 / 成分股 | akshare（东方财富） |
| 行业资金流 | akshare `stock_sector_fund_flow_rank` |
| 新闻 | akshare（财联社电报 / 东财 7×24） |
| 限售解禁 | akshare `stock_restricted_release_detail_em` |
| **财经日历（事件日历）** | **东方财富 `RPT_CPH_FECALENDAR` 报表**（真实事件表）<br>+ 规则推算（股指期货交割日）+ `release.parquet`（解禁）+ 人工惯例清单 |
| 行业估值 | tushare `index_dailybasic` |
| 盘中实时快照 | 东方财富 `push2.eastmoney.com`（JSONP 直连） |

所有接口均为公开免费通道，可用性不保证；仅用于个人研究。
