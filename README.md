# 税务稽查案件与复议流程

纯Python标准库实现的税务稽查案件与复议流程原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、补税、滞纳金、处罚和证据完整性和冲突检查。
- `src/preservation.py`：税收保全规则，财产标的、复核生效、唯一占用和到期判断。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：演示页面，可完成保全提出、复核和解除。
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
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

## 税收保全

- `POST /api/records/{id}/preservations`：调查人员提出保全，请求体为`{"asset_type":"bank_account|real_estate|vehicle","asset_key":"...","amount":100000,"duration_days":30,"reason":"..."}`，提出后为`pending`，不占用额度。
- `POST /api/preservations/{id}/actions/review`：负责人复核，请求体为`{"approve":true,"note":"..."}`，通过后生效（`active`）并计算到期时间；驳回需填写意见。
- `POST /api/preservations/{id}/actions/renew`：续保，请求体为`{"extend_days":30,"reason":"..."}`，到期后不允许。
- `POST /api/preservations/{id}/actions/release`：解除，请求体为`{"reason":"..."}`，到期后只允许解除，解除记录保留。
- `POST /api/preservations/{id}/actions/seize`：转为扣划，请求体为`{"note":"..."}`，到期后不允许。
- `GET /api/records/{id}/preservations`：保全列表与占用汇总；`GET /api/records/{id}`详情附带`preservation`汇总（当前占用额度、待复核与已到期待解除待办）。

约束：同一银行账户、不动产权证或车辆车架号在`pending`/`active`期间全库唯一占用；保全金额累计不超过案件欠缴总额（复核通过时会按最新欠缴总额复查）；提出、复核、续保、解除、扣划均写入案件审计时间线。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
