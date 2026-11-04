# data/predictions.csv 结构（v2）

v2 在 v1 的 15 列后追加 5 列；读写统一走 `ah.settle.read_ledger / write_ledger`（缺列取默认值，未知列保留在末尾）。
旧 15 列文件仍可直接读取、结算、统计；任何写回都会把表头升级到 v2。完整迁移（恢复原信心、回填 final_*）用：

```
python -m ah.migrate_csv --check   # 只检查（有待迁移项时退出码 1）
python -m ah.migrate_csv           # 就地迁移，幂等
```

| 列 | 类型 | 含义 / 默认 |
|---|---|---|
| date … settled | — | v1 原有 15 列，含义不变。`line` 为所选一方视角（负=让球）；`confidence` 现在**始终是原信心** high/medium/low |
| odds_format | `hk` \| `decimal` | `odds_if_known` / `final_odds` 的格式。缺省 `hk`。结算：hk 赢 = 水位，decimal 赢 = 赔率−1 |
| odds_estimated | `true` \| `false` | 无 `odds_if_known`、按 0.9 港盘水位估算盈亏时为 `true` |
| status | `active` \| `void` \| `cancelled` | `cancelled` = 赛前撤销；`void` = 比赛取消/腰斩等作废；二者一律 void、0 单位。缺列时：旧 `confidence=cancelled` → `cancelled`，否则 `active` |
| final_line | 数字或空 | 撤销/调整时的盘口（所选一方视角） |
| final_odds | 数字或空 | 撤销/调整时所选一方的水位（格式同 odds_format） |

范围检查（`ah.settle.check_odds`，logger `ah.settle` WARNING，只报警不拦截）：hk ≥ 1.5 或 ≤ 0、decimal < 1.2。

stats.json 新增：每个分组桶的 `cancelled`（撤销场数，已计入 void，不计入命中率）与 `odds_estimated`（估算水位场数）；
顶层 `odds_estimated` {picks, decided, units, match_ids, note}；`series[].odds_estimated`。
`by_confidence` 按原信心归类（撤销行不再落入「取消/其他」，只有缺原信心的旧数据才会）。
