# 板块评分工作台

借鉴「帽子哥」投资分析框架（分批-纪律型 + 跷跷板轮动 + 解禁筹码供给 + 日式低增长应对）构建的
**A 股行业板块量化评分与新闻工作台**。

- 每个交易日 **14:00** 自动评分（盘中快照），15:30 收盘复核归档
- 手机浏览器打开即用，可「添加到主屏幕」当 App 用
- 全免费数据源，零服务器（GitHub Actions + GitHub Pages）
- 12 维评分 + 宏观分组成决策矩阵，输出可执行动作而非单一信号

> 评分仅作研究参考，不构成投资建议。详见 [DISCLAIMER.md](DISCLAIMER.md)。

---

## 一、它是怎么跑的

```
GitHub Actions（免费定时执行）
  13:40  warmup  抓历史K线增量 / 解禁日历 / 新闻池
  14:00  score   抓盘中快照 -> 12维评分 -> 生成 JSON
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
python pipeline/sync_boards.py     # 生成 pipeline/config/sector_meta.json
```

把生成的 `sector_meta.json` 提交上去。这一步会把实时板块清单与 `keywords_seed.json`
合并，并为缺少关键词的板块补默认值（脚本会提示哪些板块建议补充关键词）。

### 5. 手动跑一次数据

`Actions → daily-score → Run workflow`

- 第一次：stage 选 `warmup`（下载约 560 天历史 K 线，耗时较长）
- 第二次：stage 选 `score`

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

## 七、关于数据源的可用性（重要）

代码内置**双通道**：优先 akshare，失败自动回退东方财富公开接口直连。

原因：实测 akshare 部分接口硬编码了分片域名（例如 `17.push2.eastmoney.com`），
在部分网络环境（校园网、企业网、部分云主机）下**该域名不可达**，而
`push2.eastmoney.com` 正常。双通道可避免此类环境直接跑挂。

如果两个通道都失败，说明该网络对东财接口整体不通，此时：

1. 换网络环境重试；或
2. 让 GitHub Actions 跑（CI 出口在境外，通常可正常访问）；或
3. 用 `python pipeline/make_demo.py` 先生成合成数据把界面调通

## 八、已知待补项

| 项 | 说明 |
|---|---|
| 权重为**先验值** | 尚未用真实历史数据校准，需积累快照后用 `backtest.py` 调优 |
| 行业估值映射 | tushare 走申万行业代码，与东财板块尚未建立完整映射；未命中时自动用价格分位代理 |
| 尾盘/盘中差异 | 14:00 评分基于未完成 bar，收盘后数值会变化 |
| 板块成分股为快照 | `stock_board_map` 缓存 7 天，成分股调整期内可能短暂不准 |

> 以下已于 P1 补齐：宏观四要素真实数据（`macro.py`）、解禁精确归属与减持计数（`chips.py`）、
> 真实交易日历、评分回测与 IC 分析（`backtest.py`）、前端 IndexedDB 缓存。

## 九、数据来源

| 数据 | 来源 |
|---|---|
| 板块行情 / 历史K线 / 成分股 | akshare（东方财富） |
| 行业资金流 | akshare `stock_sector_fund_flow_rank` |
| 新闻 | akshare（财联社电报 / 东财 7×24） |
| 限售解禁 | akshare `stock_restricted_release_detail_em` |
| 行业估值 | tushare `index_dailybasic` |
| 盘中实时快照 | 东方财富 `push2.eastmoney.com`（JSONP 直连） |

所有接口均为公开免费通道，可用性不保证；仅用于个人研究。
