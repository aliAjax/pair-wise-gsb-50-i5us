# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚、证据完整性和冲突检查，以及税收保全的标的校验、期限与金额上限规则。
- `src/repository.py`：SQLite建表、事务和查询，含保全标的唯一占用索引与到期清扫。
- `src/service.py`：用例编排、权限检查、乐观并发、审计和案件保全摘要。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8326
```

默认端口为`8326`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，附带`preservation`保全摘要（当前占用额度、待复核金额、已扣划合计、可用额度与待办）。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 税收保全

稽查中发现纳税人转移财产迹象时，调查人员（`inspector`）可提出税收保全，负责人（`reviewer`）复核通过后生效：

- 财产标的支持银行账户、不动产权证、车辆车架号三类，编号按类型校验格式。
- 同一标的同一时间只能被一个案件占用（待复核和生效中都算占用），解除、驳回、到期或转扣划后释放。
- 保全金额合计（含待复核）不得超过案件欠缴总额`total_due`，提出和复核通过时都会校验。
- 到期日由`duration_days`或`expires_on`指定；到期后措施自动失效，不能再续保或转为扣划，只能办理解除，解除记录永久保留。
- 提出、复核、续保、解除、转为扣划、到期失效均写入案件审计时间线。
- 已结案案件不能再采取保全措施。

### 保全接口

- `POST /api/records/{id}/preservations`：提出保全，请求体`{"target_type":"bank_account|real_estate|vehicle","target_key":"...","amount":1000,"duration_days":30,"reason":"..."}`。
- `GET /api/records/{id}/preservations`：案件保全列表。
- `GET /api/preservations/{id}`：保全详情。
- `POST /api/preservations/{id}/actions/review`：复核，`{"data":{"outcome":"approved|rejected","note":"..."}}`。
- `POST /api/preservations/{id}/actions/renew`：续保，`{"data":{"duration_days":30,"reason":"..."}}`。
- `POST /api/preservations/{id}/actions/release`：解除，`{"data":{"reason":"..."}}`。
- `POST /api/preservations/{id}/actions/convert`：转为扣划，`{"data":{"note":"..."}}`。

演示页（`GET /`）可完成创建案件、提出保全、复核、续保、解除和转为扣划，并展示案件时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝、版本冲突，以及税收保全的提出复核、重复占用、金额上限、到期限制、续保解除扣划留痕。
