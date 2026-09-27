# Smart Home Rule Engine (Python / FastAPI)

本地可运行的智能家居自动化服务。**不连接真实硬件、MQTT 或云平台**，
设备状态与命令全部通过 REST API 在进程内模拟，数据落盘到本地 JSON 文件。

## 快速开始

```bash
uv sync                      # 安装依赖
uv run uvicorn main:app      # 启动服务（默认 127.0.0.1:8000）
uv run pytest                # 运行全部自动化测试
```

数据文件默认写到 `data/store.json`，可用环境变量 `SMART_HOME_DATA` 覆盖。

调用方通过请求头 `X-Api-Key` 认证（本地模拟，见 `app/auth.py`）：

| API key     | 调用方 | 权限范围                          |
|-------------|--------|-----------------------------------|
| `admin-key` | admin  | 全部设备/规则 + 注册设备 + 查审计 |
| `alice-key` | alice  | 设备 lamp-1, sensor-1；规则 rule-1, rule-2 |
| `bob-key`   | bob    | 设备 lamp-2；无规则               |

## 架构

```
main.py                 FastAPI 装配、统一错误响应
app/models.py           领域模型（设备/命令/规则/执行记录/审计）
app/storage.py          JSON 原子落盘（临时文件 + os.replace）
app/migrations.py       存储 schema 版本迁移
app/auth.py             API key 认证与授权范围
app/services/           device_service / rule_service / audit（业务核心）
app/routers/            devices / rules / audit（HTTP 层，只做鉴权与转发）
app/deps.py             单例装配；规则动作经设备服务执行
```

路由层只做认证鉴权，业务规则全部在服务层，便于扩展与排查。

## 设备状态与命令语义

- **状态上报** `POST /devices/{id}/state`：携带单调递增的 `state_version`。
  - `state_version <= 当前版本` → `409 stale_data`（过期数据，明确拒绝）；
  - 属性不在设备 `allowed_properties` 白名单内 → `422 validation_failed`（非法状态）；
  - 所有上报在同一锁内串行化，**并发到达时最终状态只取决于版本号，与顺序无关**。
- **命令下发** `POST /devices/commands`：
  - 携带 `idempotency_key` 的重复提交返回**首次执行结果**（同一 `command_id`），不重复执行；
  - 设备离线 → 命令确定性置为 `rejected` 且 `reason=device_offline`，结果可查询；
  - 同设备存在内容不同的待执行命令 → `409 conflict`；
  - `action="set"` 会把 `params` 合并进设备状态并递增 `state_version`。
- 设备注册幂等：重复注册返回已存在设备，不覆盖状态。

## 自动化规则语义

- 规则 = 触发器（设备属性满足 `eq/ne/gt/lt` 条件）+ 动作列表（对设备下命令）。
- **事件去重**：事件键为 `{device_id}:{state_version}`，已处理事件键持久化到磁盘，
  同一事件重复投递、服务重启后重放都**不会重复执行**。
- **静态校验**（创建/更新时）：
  - 引用不存在的设备 → `422` 并指明缺失设备；
  - 规则间按"动作写属性 → 触发其他规则"建图，出现环 → `422` 报循环依赖路径。
- **版本可追溯**：规则每次更新 `revision + 1`；执行记录保存当时的
  `rule_revision` 与触发快照（输入状态、版本号），历史执行可复现。
- 规则动作由规则引擎以 `rule:{rule_id}` 身份经设备服务执行，动作失败也会记入执行结果；
  规则动作引起的级联触发有深度上限（8），防止失控。

## 权限边界

- 每个调用方只能操作授权范围内的设备与规则；列表接口按范围过滤。
- 越权访问返回 `403` 并**写入审计日志**（`GET /audit/`，仅管理员可查）。
- 设备注册、审计查询仅管理员可用。

## 旧数据兼容

存储文件带 `schema_version`（当前为 2）：

- 无版本号视为 v1，启动时自动迁移：设备补 `allowed_properties=null`、
  规则补 `revision=1` 等默认值，**原有字段语义不变**；
- 版本低于最低支持版本或高于当前版本 → 拒绝加载并说明原因（`MigrationError`），
  不做不安全的猜测式迁移。

## 本地验证

```bash
uv run pytest -v          # 全部测试，日志打印输入/关键状态/判定依据
```

覆盖：并发状态上报、重复命令幂等、重复事件、重启后不重复执行、
离线设备拒绝、规则循环依赖、引用缺失、越权访问与审计、v1 旧数据迁移与拒绝策略。

手工冒烟示例：

```bash
uv run uvicorn main:app &
curl -X POST localhost:8000/devices/ -H 'X-Api-Key: admin-key' -H 'Content-Type: application/json' \
     -d '{"device_id": "lamp-1"}'
curl -X POST localhost:8000/devices/lamp-1/state -H 'X-Api-Key: admin-key' \
     -H 'Content-Type: application/json' \
     -d '{"device_id": "lamp-1", "state": {"power": "on"}, "state_version": 1}'
curl localhost:8000/devices/lamp-1 -H 'X-Api-Key: admin-key'
```
