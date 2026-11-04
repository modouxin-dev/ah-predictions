"""先知 · 模型模块（模型研究员负责）。

- ah_math:   比分矩阵 -> 亚盘赢/赢半/走/输半/输概率、EV、公平盘口；去抽水（multiplicative / power / Shin）。
- market:    Pinnacle 市场基线：无水公平价 + 我方价格的 edge（门槛函数 market_gate）。
- baseline / dixon_coles / blend: 研究用模型（未接入每日发布）。
"""
MODEL_VERSION = "v0"
