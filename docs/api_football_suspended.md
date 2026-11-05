# API-Football 停用（2026-11-05）

账号返回：`Your account is suspended, check on https://dashboard.api-football.com.`

## 本地熔断
1. 环境变量 `AH_API_FOOTBALL_DISABLED=1`（收盘循环已导出）
2. `data/cache/api_football_disabled.json`（遇到 suspended 自动写入）
3. `config/sources.yaml` → `api_football.enabled: false`

任一生效则 `Client.enabled=False`，`get()` 直接报错，**不发网络请求**。

## 恢复步骤（需用户确认账号已解封）
1. 仪表盘确认 active
2. 删 `data/cache/api_football_disabled.json`
3. `sources.yaml` 改 `enabled: true`
4. 去掉循环脚本里的 `AH_API_FOOTBALL_DISABLED`
5. 用 `/status`（不计次）验证一次

## 停用期间替代
| 用途 | 替代 |
|---|---|
| 赛程 | titan007（`fetch_fixtures --source auto`） |
| 亚盘 | OddsPapi（主）+ The Odds API；InferSports 仍 opt-in |
| 首发 | J.League 官网（有映射的场次）；其余暂无/unverified，不单源签发 |
| 比分 | football-data（欧洲）+ AFC 内部交叉核对 + J.League |
| 伤停 | 暂无可靠替代 |

## 备用 key / 注册（待用户确认，未执行）
- 现有密钥名只有 `API_FOOTBALL_KEY`，箱内无第二把 key。
- 选项 A：用户在原账号仪表盘申诉解封。
- 选项 B：新注册 api-sports 免费账号（新邮箱），把新 key 写入 vault/`ah-secrets.env` 的 `API_FOOTBALL_KEY`（会覆盖旧 key；需先知+用户确认）。
- 选项 C：升级付费计划（api-football.com 定价以官网为准，本岗不擅自付费）。
- 不要用同一信用卡/设备批量开号规避停用（有再次封禁风险）。
