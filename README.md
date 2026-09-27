# Smart Home Rule Engine（本地模拟版）

一个可在本地完整跑起来的 FastAPI 智能家居自动化服务。**不接真实硬件、MQTT 或云平台**：
设备是本地 JSON 文件里的记录，状态上报/命令下发由本服务内的“模拟设备”直接处理。
功能包括设备管理、确定性的状态与命令语义、自动化规则引擎、权限边界与全程审计留痕。

## 1. 快速开始

```bash
uv sync --group dev          # 安装运行与测试依赖
uv run python main.py        # 启动服务，默认 http://127.0.0.1:8000
# 或：uv run uvicorn app.main:app --reload
# 数据目录可用环境变量覆盖：SMART_HOME_DATA=/path/to/data uv run python main.py
```

打开交互式文档：<http://127.0.0.1:8000/docs>

所有接口都需要请求头 `X-Caller-Id: <调用方id>`（本地模拟身份，见 `data/callers.json`）：

| caller_id | 角色 | 可访问设备 |
|---|---|---|
| `admin` | admin | 全部设备、全部规则 |
| `alice` | user | `light-living`、`sensor-hall`、`switch-fan` |
| `bob` | user | `lock-door` |

本地数据文件（都在 `data/` 下）：

* `state.json` —— 设备、命令记录、规则、规则执行记录、事件去重表（原子写入）。
* `audit.jsonl` —— 追加式审计日志，每行一条 JSON，只追加不改写。
* `callers.json` —— 调用方与授权范围（从 `callers.example.json` 复制修改）。

## 2. 代码结构（分层，逻辑不堆在路由里）

```
app/
  errors.py    # 统一业务错误码 + FastAPI 异常处理（结构化错误响应）
  models.py    # 领域类型与“各设备类型允许的状态属性”规格（唯一事实来源）
  store.py     # JSON 文件持久化：单锁串行化、原子写、schema 版本迁移
  audit.py     # JSONL 追加式审计
  auth.py      # 调用方注册与授权判定
  devices.py   # 设备服务：注册 / 查询 / 状态上报 / 命令下发 / 上下线
  rules.py     # 规则服务（CRUD）+ RuleEngine（事件评估、去重、循环检测）
  container.py # 依赖组装（单一服务实例来源），deps.py 提供 FastAPI 依赖
  routers/     # 仅做 HTTP 编解码与转发的薄路由层
tests/         # 本地自动化测试（并发、幂等、离线、循环、越权、旧数据迁移）
```

排查问题的入口：结构化错误码（响应体 `error.code`）、设备 `version` 与 `last_report_seq`、
规则执行记录里的 `trace`（逐条条件与动作的判定依据）、`audit.jsonl` 里每次授权裁决。

## 3. 设备状态与命令语义

### 3.1 设备类型与合法状态

合法状态字段由 `app/models.py` 的 `DEVICE_STATE_SPEC` 集中定义，**上报与命令共用同一校验**：

| 类型 | 合法属性 |
|---|---|
| `light` | `power: bool`，`brightness: int 0..100` |
| `switch` | `power: bool` |
| `thermostat` | `power: bool`，`target_temp: float 10..35`，`mode ∈ {heat,cool,auto}` |
| `sensor` | `temperature: float -40..85`，`humidity: float 0..100`，`motion: bool` |
| `lock` | `locked: bool` |

未知字段、越界值、类型不符一律 `422 validation_failed`，且在校验通过前不触碰任何已存状态。

### 3.2 状态上报（POST `/devices/{id}/reports`）

请求体：`{"state": {...}, "seq": <每设备单调递增整数>, "token": "<上报幂等键>"}`。

判定顺序（在全局写锁内，因此并发上报有确定的唯一顺序）：

1. **重复提交（同 `device_id` + 同 `token`）**：不改变状态、不重复触发规则，
   回显首次结果，`reason="duplicate"` 和首次产生的 `rule_execution_ids`。
2. **过期数据（`seq < 已接受的 last_report_seq`）**：拒绝 `409 stale_state`。
3. **序号冲突（`seq == last_report_seq` 但 token 不同）**：拒绝 `409 stale_state`，
   响应中给出已接受的 token，便于区分“旧数据重试”与“同序号冲突”。
4. 否则接受：合并 state、`version += 1`、更新 `last_report_seq/token`；若状态实际变化，
   在同一把锁内触发规则引擎。

并发结论：N 个不同 seq 同时上报时，只有最大 seq 生效（`version` 只加 1），其余全部 stale；
同一 token 并发时只有一次真正生效，其余回显 duplicate。测试：`test_devices.py`。

### 3.3 命令下发（POST `/devices/{id}/commands`）

请求体：`{"command_id": "<客户端生成的幂等键>", "device_id": "...", "params": {...}}`。

* **重复命令（同 `command_id`）**：不重复执行，回显首次裁决（含 applied/rejected 与原因），
  响应 `duplicate=true`。并发重复提交同样保证只执行一次。
* **离线设备**：先用 `/devices/{id}/status` 置 `offline` 模拟离线。此时命令被
  **拒绝但留痕**（命令记录 `status=rejected, reason=device_offline`），接口返回
  `409 device_offline`；对同一 `command_id` 的重试幂等回显该拒绝记录。
* **应用成功**：模拟设备立即采纳参数为自身状态，`version += 1`，该状态变化同样可能触发规则。

冲突/非法参数不会被静默接受：非法参数 `422`，离线 `409`，目标不存在 `404`。

## 4. 自动化规则

规则结构：

```json
{
  "rule_id": "r1", "name": "人来灯亮", "enabled": true,
  "conditions": [{"device_id": "sensor-hall", "property": "motion",
                  "op": "eq", "value": true}],
  "actions": [{"device_id": "light-living", "params": {"power": true}}]
}
```

* 条件支持 `eq/ne/gt/ge/lt/le`；多条件为 **AND**，事件发生后在一致快照上统一判定。
* 只有“事件设备出现在条件中 **且** 事件实际改变了规则关注的属性”才评估该规则。
* 动作以系统身份 `rule:<rule_id>` 下发为内部命令（带独立 `rcmd-*` command_id），
  每个动作的结果（applied / rejected + 原因）都写进执行记录。

### 4.1 同一事件绝不重复执行

事件键：上报事件为 `dev:<device_id>:<seq>`，命令触发事件为 `cmd:<command_id>`。
已处理事件保存在 `state.json` 的 `processed_events` 中：

* 重复上报（同 token）、服务重启后再次投递同一事件：直接回显已有 execution id，
  **不会产生新执行、不会重复下发动作**。
* 规则动作引起的连锁状态变化仍会评估其他规则，但同一事件键有重入保护；
  规则在创建/更新时做循环检测，运行时另设级联深度上限（`MAX_ACTION_DEPTH=8`）兜底。

### 4.2 循环依赖与悬空引用（创建/更新时即报错，不接受后补）

* 动作目标或条件监听的设备不存在 → `422 broken_reference`（details 给出 `missing_device`）。
* 规则动作写的设备被另一规则监听，构成环（含“自己监听又自己写”的自环）
  → `422 rule_cycle`，details 中给出环路路径，规则不落库。
* 规则更新（PUT `/rules/{id}`）会使 `version += 1` 并重新做上述全部校验。

### 4.3 历史结果可追溯复现

每次规则评估生成一条不可变 `RuleExecution`（GET `/rules/{id}/executions`），其中包含：

* `rule_version` 与 **完整 `rule_snapshot`**（评估那一刻的规则内容）；
* `matched`、每个条件/动作的 `trace` 文字判定依据、动作结果、事件状态快照。

规则被更新或停用后，历史 execution 仍按当时版本的规则展示，可逐条复现“为什么触发/没触发”。

## 5. 数据版本与旧数据兼容规则

`state.json` 内含 `schema_version`，由 `app/store.py` 明确定义迁移步骤：

* **v1 → v2（当前版本）**，语义稳定且明确：
  * 设备 `online: bool` → `status: "online"|"offline"`；迁移设备 `version=0`、无已接受 seq；
  * 规则 `trigger{device_id,property,equals}` → 一个 `eq` 条件；
    `target{device_id,set}` → 一个动作；`title→name`、`active→enabled`。
* **无法安全迁移则拒绝启动**（`500 data_migration_failed`，原因写进消息/details）：
  * 文件缺 `schema_version` 或不是整数；
  * 数据版本 **高于** 软件支持的最高版本（防止旧软件误读新数据造成语义丢失）；
  * v1 数据含未知设备类型、状态未知字段、类型不符或越界（不做猜测性截断/默认值）。
* 正常加载后首次写入即把文件重写为 v2；`processed_events`/`report_tokens` 从 v2 开始累积，
  因此迁移前不会虚构任何“已处理事件”。

## 6. 权限边界

* 每个请求必须携带有效的 `X-Caller-Id`，缺失/未知 → `401 unauthorized`。
* `admin` 可操作全部设备与规则；普通调用方只能操作 `allowed_devices` 内的设备、
  以及自己创建的规则（`rule.created_by == caller_id`）。
* 越权读取、上报、下发命令、查看/修改他人规则、创建“驱动他人设备/监听他人设备”的规则
  一律 `403 forbidden`；**每次允许与拒绝都会写一条审计**（含原因与关键参数）。
* 查询审计：GET `/audit?limit=100`（最新在前）。被拒绝的命令不会落库执行，只留审计。

错误响应统一形如：

```json
{"error": {"code": "stale_state", "message": "...", "details": {...}}}
```

错误码一览：`unauthorized(401)`、`forbidden(403)`、`not_found(404)`、
`conflict(409)`、`stale_state(409)`、`duplicate_command(409)`、`device_offline(409)`、
`validation_failed(422)`、`rule_cycle(422)`、`broken_reference(422)`、
`data_migration_failed(500)`。

## 7. 本地验证方法

```bash
uv run pytest                      # 全量自动化测试（日志实时打印输入/关键状态/判定依据）
```

测试覆盖（`tests/`）：

* `test_devices.py` —— 并发不同 seq 上报、同 token 并发、重复命令并发、
  离线命令拒绝+留痕+重试幂等、非法状态拒绝。
* `test_rules.py` —— 事件仅触发一次、重启后重复事件、并发同事件、
  多规则环与自环、悬空引用、更新后历史快照可复现、动作目标离线的 trace。
* `test_authz.py` —— 设备/规则越权（含审计 denied 断言）、未知调用方。
* `test_migration.py` —— v1→v2 迁移语义、版本过高拒绝、未知类型/非法旧状态拒绝。
* `test_http_api.py` —— 经 FastAPI 栈的端到端错误信封与幂等行为。

每个用例通过 `log_verdict(...)` 输出三行：`输入=... / 关键状态=... / 最终判定依据=...`；
业务日志（`smart_home.devices`、`smart_home.rules`）同时打印每次上报、命令与规则评估过程。

手动联调示例：

```bash
H='X-Caller-Id: alice'
curl -s -X POST localhost:8000/devices -H "$H" -H 'Content-Type: application/json' \
  -d '{"device_id":"sensor-hall","name":"Hall","type":"sensor"}'
curl -s -X POST localhost:8000/devices/sensor-hall/reports -H "$H" \
  -H 'Content-Type: application/json' \
  -d '{"state":{"motion":true},"seq":1,"token":"evt-1"}'
curl -s "localhost:8000/audit?limit=20" -H "$H"
```

## 8. API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/devices` | 注册设备（admin，或注册到自己授权清单内的设备） |
| GET | `/devices` / `/devices/{id}` | 列出/查询设备（按授权过滤） |
| POST | `/devices/{id}/status` | 模拟上线/离线 `{"status":"online|offline"}` |
| POST | `/devices/{id}/reports` | 状态上报（seq + token 幂等/时序语义） |
| POST | `/devices/{id}/commands` | 命令下发（command_id 幂等） |
| POST/GET/PUT | `/rules`、`/rules/{id}` | 规则创建/查看/列表/更新 |
| GET | `/rules/{id}/executions` | 规则历史执行（含规则快照与 trace） |
| GET | `/audit?limit=` | 授权与关键操作审计 |
