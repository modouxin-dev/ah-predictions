# research/ — 模型研究员（先知 · 亚盘）

只做研究与离线输出，**不接入每日发布流程**。所有数字都必须由本目录脚本运行得到，不手填。

## 环境
- 使用仓库 `.venv`。研究依赖已写入根目录 `requirements.txt`（numpy/scipy/pandas/penaltyblog 钉死到 .venv 版本）。（`src/ah/model/` 的生产函数只依赖 numpy/scipy；`dixon_coles.py` 惰性导入 penaltyblog）。

## 文件
| 文件 | 作用 |
|---|---|
| `fd_data.py` | football-data.co.uk 下载（缓存 `research/data/raw/`，gitignore）与统一读取 |
| `coverage.py` | 数据覆盖表 → `outputs/coverage.md` |
| `calibrate_dc.py` | 模型 B（DC）温度/等渗 walk-forward 校准对比 → `outputs/calibration_dc.md` |
| `backtest.py` | 滚动验证：Elo 基线 / Dixon-Coles / DC+Elo / 融合 / Pinnacle 市场 → `outputs/backtest_results.md` |
| `paper_trades_model_p.py` | 审计员模拟单账本的“基准”概率 → `outputs/paper_trades_model_p_<时间戳>.csv` |

## 模拟单账本输出（与回测审计员约定，2026-11-04）

**版本**：`model_version = market-baseline-v0`，标签“基准”。只用 Pinnacle 去抽水市场价（按比例法），
不含任何自有模型。按定义，市场基准相对 Pinnacle 自身的 edge≈0（`ev_at_pinnacle` 恰为负的抽水份额），
`pick_side` 只是“期望结算得分更高的一方”（同分时取 EV 损失更小的一方），**仅作基准，不构成推荐**。

**输入**：只读 `/workspace/ah-audit/data/paper_trades.csv`；赔率来自本仓库 `data/cache/odds/`（API-Football `/odds`，
Pinnacle “Asian Handicap” 与 “Goals Over/Under 2.5”）。使用账本 `home_line` 对应的 Pinnacle 那条线的主客价格。
缺少 API-Football fixture id、缺 Pinnacle 亚盘快照、账本无盘口或 Pinnacle 无此线 → `decision = 数据缺失，不推`，
概率与 `model_p` 留空，不做任何猜测。

**字段**
- `match_id`：与账本 match_id 完全一致（`apif-<fixture>` 用于对接 API-Football）。账本若无 match_id，
  对接键为 `date + home + away + kickoff_utc`。
- `pick_side`（home|away）、`line`（推荐方视角）、`home_line`（主队视角）。
- `p_win, p_half_win, p_push, p_half_loss, p_loss`：推荐方视角 5 种结算概率（由 Pinnacle 亚盘 + 大小 2.5 去抽水后
  反解 Poisson 隐含比分矩阵，再按四分之一盘拆半注结算）。
- `model_p`：期望结算得分 = P赢 + 0.75·P赢半 + 0.5·P走 + 0.25·P输半。
- `generated_at`：生成时间，北京时间 ISO 8601（+08:00）。
- `captured_at`：本地抓取该快照的时间（缓存写入时刻，+08:00）；`bookmaker_updated_at`：API-Football 返回的公司更新时间；
  `stage`：快照所处阶段（当前为日报时点 report）。
- `valid_prekickoff`：`generated_at < 开赛` 且 `captured_at < 开赛` 才为 true；否则为 false 并在 `note` 说明，不隐藏。
- 另含 `pin_home_odds, pin_away_odds, pin_margin, p_over25_devig, ev_at_pinnacle, odds_source` 便于复核。

**防事后**：每次运行生成新文件，文件名带北京时间戳（`paper_trades_model_p_YYYYMMDDTHHMM+0800.csv`），
已存在则拒绝写入；审计员取开赛前最后一个版本。

**校验（代码强制，`src/ah/model/scoring.py::validate_probs`，测试 `tests/test_model_scoring.py`）**
- 整数盘：P赢半 = P输半 = 0；四分之一盘：P走 = 0；半球盘：走/赢半/输半均为 0；五项和为 1（容差 1e-9）。

**评估口径（审计员已同意）**
- 结算得分 y：赢 1、赢半 0.75、走 0.5、输半 0.25、输 0（走盘**不剔除**）。
- Brier = mean((model_p − y)²)，只算已结算场次，按推荐方视角（`scoring.brier_settled`）。
- 审计员做 model_p 分箱校准（各箱 model_p 均值 vs y 均值）；此账本暂不算 log loss。

## 回测口径（backtest.py）
见脚本 docstring；`outputs/backtest_results.md` 为最近一次运行结果。注意回测中的亚盘 log loss 用的是
“剔除走盘的主队赢/输”二元口径，与上面账本的结算得分口径不同。

## 置信度阈值（提案，未经回测支持）

依据 `outputs/backtest_results.md`（E0+E1 2021-07 起，AH n=3758）：所有模型（elo/dc/dcelo/blend）
log loss、Brier 均显著劣于 Pinnacle 收盘；按模型 EV 分桶（0–1%、1–3%、3–5%、>5%）下注，CLV 全部显著为负（约 −2.2% ~ −2.9%），
ROI 全部不显著，且 EV 与 CLV/ROI 无单调关系。**回测不支持任何以模型 EV 为依据的正 edge 阈值。**

提案（仅纸面交易）：
- 硬门槛：`market_gate()` 相对 Pinnacle 去水公平价 EV ≥ 0（不满足一律不推）。
- 高：市场门槛 EV ≥ +3% —— 当前仅为路线图占位值，回测无支撑；须先积累 ≥100 笔纸面交易且 CLV > 0 方可启用。
- 中：市场门槛 EV +1%~3%；低：0~1%。
- 模型（DC/Elo/blend）只作方向参考，不参与置信度分级。数值见 `config/model.yaml: confidence_proposal`。

## 基准纸面交易 v2 数据源（20261104T2121 起）

- Pinnacle 亚盘价格改读数据官盘口快照 CSV（`stage=report, bookmaker=pinnacle`，主队视角 `ah_home_line`，负数=主让）。
  读取器 `src/ah/model/snapshots.py`：新路径 `data/snapshots/odds/` 优先、旧路径 `data/odds_snapshots/` 兼容；
  可 `--snap-rev <提交>` 用 `git show` 只读读取。同一盘口多来源时取 oddspapi > api_football > odds_api，同源取最晚 captured_at，
  且 captured_at / price_ts 必须早于开赛。
- 本次运行：`research/paper_trades_model_p.py --snap-repo /workspace/ah-data --snap-rev 94280a0 --report-date 2026-11-05`。
- 新列 `margin` = 1/主赔 + 1/客赔 − 1；`margin_flag=high` 当 margin > 5%（Pinnacle 亚盘通常 2–4%，疑似陈旧价，待数据官核查），仍输出 model_p。
- 快照无大小球：大小 2.5 只用 data/cache/odds 的赛前 Pinnacle 价，没有则总进球按 2.6 假设（`total_source` 列标明；只影响整数/四分之一盘的走盘/半赢半输拆分）。
- 旧文件 `paper_trades_model_p_20261104T2101+0800.csv` 保留不动。
