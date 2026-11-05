# 交叉验证打标口径（2026-11-05 用户确认）

只打标，**不自动拦截签发**。双源 / 主线 3 家 / 至少 1 家 verified 不放宽；stale 不放到 2h/6h；min_verified 不为 0。

## 阶段
- 有 close 数据：赛前/收盘看 **close**，不要用旧 report 否决新 close。
- 尚无 close：早报/补筛看当时的 **report**。
- **open** 只记录，不进 verdict。
- 空阶段 = `pending`，不进 withhold，也不算失败。

## 来源
- 默认排除 suspended 的 `api_football`（见 `exclude_sources`）。
- 双源但公司集合不相交：标 `no_shared_book`，不是价格冲突。

## 时效
- 时钟：评估时刻 `now`；未传则用该阶段**最新** `captured_at`。
- 只看主线 **Pinnacle** 与 **verified** 公司的 `price_ts`；没有这两类时才看全部主线。
- 不要用全场最旧一家一票否决，也不要用第一条 main 的 captured_at 当 now。
- 阈值：report 30min 降级 / 2h 撤回；close 10min 降级 / 30min 撤回（未放宽）。

## verdict（advisory）
- `ok`：当前用途阶段通过打标。
- `review`：当前阶段未过打标（含深盘标记），**不自动拦截**，由先知签发。
- `pending`：当前阶段还没有数据。
- 深盘 `deep_line` 只打标；正式拦截仍在先知侧。

## 未做
- 未改主线、未改行级 verified、未恢复 API-Football。
