# 制度文件说明

| 文件 | 用途 |
|---|---|
| `expense_policy_v1_3.md` | **运行时制度**（v1.3，33 条）。`POLICY_FILE` 默认加载这一份。 |
| `expense_policy_v1.md` | v1.2 历史版本，**只用于 blind_v2 盲标包的哈希冻结与复现，不要修改**。 |

修改制度时：

1. 新版本另存为 `expense_policy_vX_Y.md`，并同步修改 `backend/app/config.py` 的 `POLICY_FILE`；
2. 不要覆盖已冻结的旧版本文件，否则 blind_v2 的 SHA-256 校验会失败；
3. 每条条款必须写非空 `keywords`，否则 `PolicyLoader` 会直接抛 `PolicyDocError`；
4. `applies_to` 的金额过滤是**单据总额**级；单条明细限额由规则引擎（`check_category_limit`）处理。
