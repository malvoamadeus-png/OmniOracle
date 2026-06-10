# PolySport tools

`tools/` 存放非核心生产链路的研究、诊断和一次性分析工具。核心产品只有：

- 后端直接跟单与跟单详情记录：`backend/packages/copytrade/` 和 `backend/src/`
- 前端展示与本地管理界面：`frontend/dashboard/`、`frontend/admin/`

## 工具边界

每个工具目录都应尽量是可复制运行的独立单元。把某个子目录复制到其他机器时，工具不应该依赖 PolySport 根目录、`backend/`、`frontend/` 或本地 Supabase/SQLite 主库。

允许的共享方式：

- 复制少量稳定 helper 到工具内部，并在 README 写明口径来源。
- 读取工具目录内的 `config/`、`runtime/`、`output/`。
- 调用 Polymarket 等公开 API。

禁止的共享方式：

- 从工具代码 import `backend.*`、`copytrade.*`、根目录业务脚本，或通过 `sys.path` 指向 PolySport 项目根目录。
- 直接读取 `backend/packages/copytrade/copytrade.sqlite`、根目录 `metrics_fresh.sqlite`、账户 TOML、生产 `.env`。
- 为了工具方便修改后端核心表结构或前端展示口径。

## 当前工具

- `sim_copytrade/`：模拟跟单、延迟跟单、参数搜索、差距诊断和报告生成。
- `smart_money_broadcast/`：地址发现、地址测评和单地址 Markdown 播报。
- `legacy_address_metrics/`：早期批量地址测评/指标入库管线，仅作历史兼容和复盘。
- `getEndDateandLiquidity/`：一次性市场流动性/结束时间探索脚本。

## 新工具放置规则

新增“模拟、测评、调研、回测、数据探索、一次性修复验证”类功能时，默认放在 `tools/<tool_name>/`。如果确实需要复用后端逻辑，先把可复用的纯函数复制或抽成工具内部模块；不要让工具 import 后端包。

`legacy_address_metrics/` 是历史兼容例外，少数旧脚本会读取 backend copytrade SQLite/accounts；新工具不要沿用这个模式。
