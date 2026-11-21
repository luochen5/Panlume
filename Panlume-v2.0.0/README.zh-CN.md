# 盘灯（pandeng）中文说明

[English homepage](README.md) ｜ [报告与模型使用说明](docs/v2.0.0报告与模型使用说明.md) ｜ [English report guide](docs/v2.0.0-report-and-model-guide.md) ｜ [验收与发布记录](docs/v2.0.0阶段8迁移与发布验收.md)

当前源码为 **Panlume 盘灯 v2.0.0 统一报告正式版**。本版保留既有业务规则、数据库结构版本 6、数据隔离和真实请求门控，新增统一事实包、完整固定模板、可选模型叙述、报告预设与导出、整页报告成果、Ollama 图形管理、10 个观察对象、MACD/RSI、四语言和 Windows 显示优化。

独立“语言 / Language”板块用于保存语言，重启后统一生效。新安装默认 English；已有配置缺少 `language` 键时继续使用简体中文。支持 English、简体中文、繁體中文和日本語；日语缺失界面文案回退英文。历史摘要、复盘、告警、审计与原始记录保留原文。

当前完整离线回归为 **498 passed**。已完成 Windows EXE 与便携启动、独立安装卸载、个人数据保留、Defender 扫描、真实 14B 摘要成功与异常回退，以及用户界面检查。所有发布包必须从本源码重新构建并计算新哈希，不能沿用历史版本产物。

建议解压到新的 `D:\pandeng-v2`，不要覆盖旧源码、个人配置和数据库。普通用户可直接使用安装包或便携版，无需安装 Python 或打开终端。报告和模型操作见 **`docs/v2.0.0报告与模型使用说明.md`**。下方保留 D1～D6 核心和原 CLI 的中文资料，其中的阶段版本号与测试计数为当时记录。

## D1～D6 功能与命令资料

盘灯是 Windows 本地 A 股行情监控与研究辅助学习项目。当前具备 YAML 配置、SQLite、模拟行情、CSV 回放、低频调度、暂停恢复、断网补数、盘中指标、确定性规则、持仓批次、固定止损、T＋1、确认日 K 的 MA20、Ollama 异步摘要安全链路、三类行情接口的离线适配器契约与范围防护、只读数据体检、东方财富一分钟严格映射、受控真实请求和 C3 盘中双探针。C4 新增 VWAP 四位小数摘要事实、分钟单向确认推进、上游只读的确定性本地每日复盘，以及带完整门控和单次审计的显式云端复盘入口。

项目只用于公开信息处理、个人研究记录和软件学习，不连接外部账户，不执行外部操作。程序不自动下单，不承诺收益。真实持仓只能由用户通过专用命令显式确认更新。本地模型只整理已筛选事件的文字，不能修改原始行情、指标、规则结论和持仓。云端继续默认关闭；本地复盘没有模型调用路径，`cloud-review` 必须另行满足逐字确认、总开关、`daily_review` 白名单和密钥配置等全部门控。真实行情默认不请求：单次命令 `akshare-live-minute` 与双探针 `akshare-live-probe` 分别要求用户当次逐字确认，并且只写独立测试库。双探针的全部分钟保持未确认，不会根据重叠结果自动修改确认状态。

## 本机环境与启动

- Windows PowerShell，普通用户权限；
- Python 3.14.10；
- 项目目录：`D:\pandeng`；
- 虚拟环境：`D:\pandeng\.venv`。

```powershell
Set-Location D:\pandeng
$Python = "D:\pandeng\.venv\Scripts\python.exe"
& $Python --version
```

直接调用虚拟环境解释器，不需要激活脚本、管理员权限或修改系统执行策略。已经完成 C3 与 AKShare 安装的用户请按 `docs/手动复盘与云端门控C4更新说明.md` 备份和复制文件，避免覆盖个人 YAML、现有数据库与桌面备份。

## 全量离线测试

```powershell
& $Python -m pytest -q
```

D6 当前预期 **388 passed**，其中 C4 核心基线为 303 项。测试使用临时数据库、模拟时钟、本地 JSON 样例、人工编写的虚拟服务商响应、已保存的东方财富收盘后公开响应、模拟模型响应和本机临时 HTTP 服务，不请求真实行情、真实 Ollama 或云端。真实适配器测试注入离线响应，不导入 AKShare；云端客户端测试只访问测试进程启动的 `127.0.0.1` 临时服务，并使用 dummy key。

第三阶段新增的关键测试名称：

```text
test_stop_touch_boundary_clear_reactivate_and_idempotent_run
test_history_gap_does_not_block_valid_current_minute_stop
test_history_gap_preserves_price_line_but_makes_context_unknown
test_missing_or_unconfirmed_target_minute_makes_price_line_unknown
```

第四阶段覆盖 R1 思考段分离、数值与身份校验、单工作线程、队列满立即回退、异常任务恢复、有限收尾、迟到结果隔离，以及版本 4 数据库副本升级到版本 5。

适配器预备代码覆盖严格 JSON 契约、三类接口范围过滤、响应代码与请求区间防护、空响应检查点隔离和两轮增量命令行验收。B 阶段新增只读数据体检及 1／5／10 分钟轮询档位提示，关键测试位于 `tests/test_json_feed.py`、`tests/test_adapter_scope.py`、`tests/test_adapter_demo_cli.py`、`tests/test_data_audit.py` 和 `tests/test_data_audit_cli.py`。

C1 新增 55 项测试，覆盖 A 股代码与交易所后缀、固定 UTC＋8、响应大小、字段和行长度、分钟股数与日线手数换算、09:30 显式排除、分钟确认边界、明确 qfq、日 K 未确认、原始行追溯、独立配置直接加载、防重跑和只读体检。对应测试为 `tests/test_provider_common.py`、`tests/test_tushare_adapter.py` 和 `tests/test_provider_demo_cli.py`。

C2 新增 30 项测试，覆盖真实保存样本的 241 行构成、240 个常规分钟、09:30 审计、手转股、14:59 零成交量、15:00 保留、13:00 拒绝、显式确认边界、重复与乱序、请求范围、空响应、错误分类、参数前置校验、独立数据库、防重跑和原始请求审计。对应测试为 `tests/test_eastmoney_adapter.py` 和 `tests/test_akshare_demo_cli.py`。

C3 新增 17 项测试，覆盖固定两次请求与 300 秒间隔、精确请求终点、10 个重叠点去重、5 个新增点、重叠内容变化与消失、无重叠、错误确认零请求、无效时间窗、第一次失败不等待、第二次失败保留第一次、等待期间 `Ctrl+C`、主库文件名保护、命令行逐字确认、独立离线演示，以及已有前一日 240 条数据的独立库跨日复用。对应测试为 `tests/test_intraday_probe.py`。

C4 新增分钟单向确认、VWAP 展示一致性、本地复盘幂等与上游只读、版本 4／5 副本升级、云端门控零请求、成功与失败单次审计、禁止重试、三段式结果校验、交易指令与收益承诺拒绝、假密钥脱敏和本机回环协议测试。对应测试为 `tests/test_minute_confirmation.py`、`tests/test_summary_content.py`、`tests/test_review.py`、`tests/test_cloud_review.py` 和 `tests/test_storage.py`。

D4 新增桌面体检完整、缺口、空数据、质量错误、非法输入前置拒绝、全表逐行只读、模型与云端零调用、质量与批次选择及小窗口滚动测试。对应测试为 `tests/test_desktop_audit.py` 和 `tests/test_desktop_window.py`；核心审计语义继续由 `tests/test_data_audit.py` 验证。

D5 新增事件与摘要完整关联、代码／日期／结果筛选、空历史、未完成状态、非法输入前置拒绝、全表逐行只读、模型原始文本不展开、模型与云端零调用、详情选择及最小窗口导航与滚动测试。对应测试为 `tests/test_desktop_history.py` 和 `tests/test_desktop_window.py`。

## 初始化与结构版本 6

```powershell
python -m pandeng.app init
python -m pandeng.app status
```

`init` 可重复执行。历史版本 5 新增 `summary_tasks`、`summary_results` 和 `model_calls`；当前版本 6 新增 `review_runs` 和 `cloud_review_calls`。旧行情、事件、日线结论、摘要、持仓批次与审计记录保持原样。更新前仍应备份数据库；自动化测试已经验证版本 4 和版本 5 原文件不变，只对副本完成升级，旧摘要、人工持仓和审计均保留。主库是否升级由用户单独决定，独立的 C4 复盘演示不需要升级主库。

常用只读查询：

```powershell
python -m pandeng.app show-config
python -m pandeng.app holdings list
python -m pandeng.app data-status
python -m pandeng.app monitor-status
python -m pandeng.app analysis-status
python -m pandeng.app summary-status
python -m pandeng.app review-status
```

这些查询不会启动轮询或模型请求。

## 东方财富一分钟离线验收（C2）

C2 将 2026-11-17 收盘后真实探针的完整 241 行保存为只读样本。离线演示明确使用 `eastmoney_post_close_capture_offline` 来源，写入全新的 `runtime/akshare-fixture-test.db`，不会访问网络或导入 AKShare：

```powershell
& $Python -m pandeng.app --config configs/akshare-fixture-test.yaml show-config
& $Python -m pandeng.app --config configs/akshare-fixture-test.yaml init
& $Python -m pandeng.app --config configs/akshare-fixture-test.yaml eastmoney-fixture-demo
& $Python -m pandeng.app --config configs/akshare-fixture-test.yaml data-audit --session-date 2026-11-17 --as-of-time 2026-11-17T15:00:00+08:00
```

新鲜独立库应保存 240 条常规分钟，09:30 特殊点留在样本审计中，14:59 零成交量行保留，15:00 保留，没有 13:00。所有分钟显式保持 `confirmed=false`，因此体检显示 `240/240`、缺口 0、质量错误 0，同时总体状态为 `unknown`。这是确认边界尚未验证时的安全结果。

真实请求使用另一个 `runtime/akshare-live-test.db`，默认命令不会触发它。C2 的单次请求与 C3 的双探针共用此实时独立库；C3 已经验证旧库存在前一日数据时可继续追加次日新分钟。当前仍未接入真实前复权日线、真实交易日历或自动轮询，也不会因为返回 15:00 或两次响应一致就自动确认最后一根分钟。C2 历史安装与错误分类见 `docs/免费行情接入C2更新说明.md`，C3 双探针保留其历史说明，当前安装以 C4 文档为准。

## 东方财富双探针离线验收（C3）

离线双探针使用专用配置 `configs/akshare-probe-fixture-test.yaml` 和嵌目录新库 `runtime/akshare-probe-fixture/akshare-live-test.db`，与已有 `runtime/akshare-live-test.db` 完全分开。它从 C2 保存样本切出两轮响应，用模拟时钟推进 300 秒，不访问网络、不导入 AKShare，也不实际等待：

```powershell
& $Python -m pandeng.app --config configs/akshare-probe-fixture-test.yaml show-config
& $Python -m pandeng.app --config configs/akshare-probe-fixture-test.yaml init
& $Python -m pandeng.app --config configs/akshare-probe-fixture-test.yaml eastmoney-probe-fixture-demo
& $Python -m pandeng.app --config configs/akshare-probe-fixture-test.yaml data-status
& $Python -m pandeng.app --config configs/akshare-probe-fixture-test.yaml data-audit --session-date 2026-11-17 --as-of-time 2026-11-17T09:45:00+08:00
```

新鲜专用库的预期结果：模拟请求 2 次，实际等待 0 秒；第一次新增 10 条，第二次新增 5 条、重叠去重 10 条；重叠一致 10，内容变化 0，第二次消失 0，第二次新增 5；最终共 15 条分钟、2 个入库批次，全部未确认。体检应显示 `15/15`、缺口 0、质量错误 0、总体 `unknown`，因为分钟和交易日历都尚未确认。已有演示数据时拒绝重跑，不覆盖或追加。

真实双探针命令为 `akshare-live-probe`，固定两次真实请求、间隔 300 秒、自动重试 0 次。只能在用户已核实的开市日，且当前时间与五分钟后的时间都落在当日交易分钟内时启动。该命令仍需当次明确授权，完整前置条件、命令、审计位置、失败与中断行为见 `docs/免费行情接入C3更新说明.md`。C4 安装和离线验收不会执行它。

## 本地每日复盘与云端门控（C4）

独立复盘演示使用 `configs/review-test.yaml`，写入新的 `runtime/review-fixture/analysis-test.db`，不复用主库或既有第三阶段演示库。首次演示且目标文件不存在时执行：

```powershell
& $Python -m pandeng.app --config configs/review-test.yaml init
& $Python -m pandeng.app --config configs/review-test.yaml analysis-demo --session-date 2026-01-07
& $Python -m pandeng.app --config configs/review-test.yaml review-daily --session-date 2026-01-07 --code "600000"
& $Python -m pandeng.app --config configs/review-test.yaml review-status
& $Python -m pandeng.app --config configs/review-test.yaml review-status --review-id 1
```

新鲜演示应生成 `data_status=ok` 的本地复盘：240 条分钟全部确认、质量错误 0、最近快照和确认日线分析均存在。相同输入再次执行会幂等返回原编号；输入证据变化时另存新复盘，旧记录不可变。无数据、质量错误、日 K 缺失或未确认时保持 `unknown`，不会补造事实。

`review-daily` 只读上游行情、日历、质量记录、指标快照、规则事件、日线结论、本地摘要和调度状态，仅追加 `review_runs`，不重新计算指标、不产生实时提醒，也不调用 Ollama 或云端。`review-status` 只读。`cloud-review` 是独立的显式命令，必须同时通过逐字确认、云端总开关、`daily_review` 白名单、API Key 配置、本地复盘存在和无既有审计等门控。每个本地复盘最多一次实际请求，失败也保存审计且不自动重试；结果必须包含“事实：”“推测：”“后续观察：”，交易指令和收益承诺会被拒绝。完整安装、独立演示、主库可选升级、错误处理和云端边界见 `docs/手动复盘与云端门控C4更新说明.md`。C4 默认验收不执行真实 `cloud-review`。

分钟确认推进现在具备通用入库能力：只有 OHLC、成交量、来源和复权口径不变时才允许 `false → true`，并保存前后审计；它仍不会自动生成真实确认边界。VWAP 的提示词事实、严格校验与固定模板统一使用四位小数字符串，原始指标值不被回写。

## Tushare 虚拟响应离线验收（C1 历史功能）

C1 没有 HTTP 客户端，不读取 Token，也没有把离线映射称为真实接入。`TushareFixtureAdapter` 只读取 `examples/provider_fixtures/tushare/offline_responses.json` 中人工编写的虚拟响应，再复用既有清洗、去重、冲突检查、SQLite 原始行追溯和只读体检。C1 当时数据库结构为版本 5；当前 C4 结构为版本 6。

```powershell
python -m pandeng.app --config configs/real-data-test.yaml show-config
python -m pandeng.app --config configs/real-data-test.yaml init
python -m pandeng.app --config configs/real-data-test.yaml provider-fixture-demo --session-date 2026-01-12 --fixture-file examples/provider_fixtures/tushare/offline_responses.json
python -m pandeng.app --config configs/real-data-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:32:00+08:00
```

新鲜独立库的预期结果：4 条 09:31～09:32 连续竞价分钟、2 条明确 qfq 但 `confirmed=false` 的日线、1 条开市日历；2 条 09:30 特殊点样例记录单独排除，不改写为 09:31。两只虚拟对象的体检均为分钟 `2/2`、缺口 0、质量错误 0，日 K 独立显示未确认。没有调度检查点属于预期，因为该命令不运行轮询器。

演示只允许写入全新的 `runtime/real-data-test.db`，Ollama 和云端必须关闭，真实持仓表也必须为空。已有数据时拒绝重跑，不覆盖或追加。不要把配置改为主库绕过隔离。确需重新验收时，正常退出程序，将旧测试库及可能存在的 `-wal`、`-shm` 移到备份目录。

`PANDENG_TUSHARE_TOKEN=` 已在 `.env.example` 中保留空白占位，但 C1 不读取它。真实分钟权限、历史深度、前复权锚点、日线最终确认语义、交易日历权限和频率限制仍须以用户本人权限中心及官方文档为准。银河证券路线继续等待正式只读行情 API 资料，没有猜写适配器。完整安装、验收和下一步见 `docs/真实行情接入C1更新说明.md`。

## 真实行情适配器离线验收（B 阶段保留）

B 阶段当时尚未绑定真实数据服务商。`JsonFeedAdapter` 使用严格的本地 JSON 样例实现分钟线、前复权日线和交易日历三个只读接口；既有调度器在清洗入库前拒绝其他代码和请求区间外的数据。空响应仍由连续性门控记为状态未知，连续检查点不会前进。C2 的东方财富适配器是新增独立入口，不改变这里的 B 阶段历史验收方式。

演示只使用全新的 `runtime/adapter-test.db`，并要求 Ollama 和云端均关闭：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml show-config
python -m pandeng.app --config configs/adapter-test.yaml init
python -m pandeng.app --config configs/adapter-test.yaml adapter-demo --session-date 2026-01-12
python -m pandeng.app --config configs/adapter-test.yaml monitor-status
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00
```

预期首轮两只样例对象共收到并新增 8 条分钟，第二轮只读取 09:35～09:36 并新增 4 条；最终保存 12 条分钟、2 条当日明确前复权日线和 1 条交易日历，两只股票的连续检查点均到 09:36。当日日线入库仅验证接口和字段，不替代既有收盘日 K 确认流程。

已有演示数据时程序拒绝重跑，不覆盖或追加。已经运行过预备版演示的用户可直接执行上面的 `data-audit`，不必重跑 `adapter-demo`，也不要删除旧测试库。确需重新验收时，先正常退出程序，再把主文件及可能存在的 `-wal`、`-shm` 一起移动到备份目录。完整更新、预期输出和错误处理见 `docs/真实行情适配器B更新说明.md`。

真实服务商仍需另行核对官方文档、授权、频率、字段单位、时区、复权和确认状态。以后只在独立真实数据测试库中验证新适配器，密钥通过环境变量提供，是否接入主库由用户另行确认。

## 只读数据体检

`data-audit` 必须显式给出交易日和带时区的当日交易整分钟。省略 `--code` 时检查当前 YAML 的全部观察对象；指定时只能填写其中一只：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00 --code "600000"
```

体检连接执行 `PRAGMA query_only = ON`，只读取日历、分钟、质量审计、检查点和日 K。它不会补数、修复冲突、计算规则、请求行情或调用模型。

分钟状态含义：

- `ok`：截至目标分钟的应有记录完整且已确认，没有区间质量错误，交易日历明确开市；
- `price_only`：历史序列存在缺口、未确认记录或质量错误，但目标分钟有效且确认，目标分钟也没有质量冲突，因此当前价格线仍可使用；
- `unknown`：目标分钟缺失、未确认或有质量冲突，价格线不可用。日历明确休市时也不开放价格线。

当日前复权日 K 独立显示 `confirmed`、`unconfirmed`、`missing` 或 `unknown`。日 K 缺失、未确认或冲突不会把完整分钟记录重新标成不完整，分钟体检通过也不会把日 K 当成已确认。检查点只作展示，没有调度检查点的 CSV 入库数据仍可依据实际分钟记录体检。

命令返回 0 表示体检顺利执行，数据健康程度以报告中的 `ok`、`price_only`、`unknown` 为准。发现数据异常时仍可正常返回 0。报告按当前数据库回看截至时刻应有的数据，不证明这些数据在当时已经实时到达；只统计截至 `--as-of-time` 的分钟质量错误，更晚的错误不会污染较早时刻的回看。

## 桌面数据体检与追溯（D4）

桌面“数据体检”复用同一个 `audit_market_data()` 核心，范围固定为当前观察清单。用户填写 `YYYY-MM-DD` 日期和 `HH:MM` 截止分钟后手动启动；09:30、12:00 等非交易分钟会在核心审计和证据查询之前被拒绝。

报告下方列出当日质量明细与相关入库批次，各最多 100 条。选择质量行可查看问题编号、类型、级别、数据时间、消息和原始 JSON；选择批次行可查看适配器、状态、创建时间及收到、接受、去重、拒绝计数。批次关联来自分钟、日线、日历的 `ingestion_run_id` 和质量记录的 `run_id`。

桌面服务在证据连接上执行 `PRAGMA query_only = ON`，没有补数、修复或写表入口，也不构造模型客户端。空数据、缺口、未确认和质量冲突按核心结论原样展示；日 K 单独报告。完整操作与 Windows 构建见 `docs/数据体检D4使用说明.md`。

## 一键整合离线演示（D6）

“离线演示”复用固定模拟行情与确定性规则提醒，在独立 `runtime/analysis-demo/analysis-test.db` 中生成固定模板摘要和本地复盘，再只读运行数据体检与事件摘要历史。完整演示包含 10 条实际实时提醒、10 条模板结果、0 次模型调用、2 条本地复盘；个人数据库不参与。

首次生成后再次点击只读取已有结果，全部数据表逐行不变。旧版或不完整演示只显示现状，不自动修复、补写、覆盖或删除；需要重跑时使用新的 `PANDENG_DATA_DIR`。后台任务活动期间阻止直接关闭，空闲退出会取消界面轮询。完整操作、预期计数和 Windows 构建见 `docs/整合发布D6使用说明.md`。

## 桌面事件与摘要追溯（D5）

“事件与摘要”页读取 `rule_events → rule_event_checks → summary_tasks → summary_results → model_calls` 已有记录。规则事件与摘要任务各最多显示 100 条；选择事件可查看指纹和确定性消息，选择任务可查看关联编号、最终摘要、输入事实、检查 payload、回退原因、校验记录及模型调用状态、耗时和错误元数据。

历史筛选支持六位代码、可选 `YYYY-MM-DD` 日期、全部结果／模型摘要／模板回退／未完成。查询连接执行 `PRAGMA query_only = ON`；页面不会重新计算规则、生成摘要、恢复任务或发起模型、云端、真实行情请求。模型请求、原始响应和 R1 思考文本不在本页展开。手动观察摘要的其他链路不因此自动纳入本页。完整操作见 `docs/事件与摘要D5使用说明.md`。

## 轮询档位

继续使用现有的 `monitor.interval_seconds`，没有新增 YAML 配置键：

```yaml
monitor:
  # 可选 60、300、600，也可填写其他 60～3600 秒的合法整数。
  interval_seconds: 300
```

| 秒数 | 显示档位 | 使用特点 |
| --- | --- | --- |
| 60 | 1 分钟档 | 检查更及时，数据请求更频繁 |
| 300 | 5 分钟（默认）档 | 继续作为默认平衡档 |
| 600 | 10 分钟档 | 请求更少，固定止损提醒可能额外延迟 |
| 其他合法值 | 自定义档 | `show-config` 显示实际秒数和折算分钟数 |

无论选择哪一档，每轮都会增量补齐中间所有一分钟数据，不会只取最新一条。轮询间隔不会改变普通模型调用规则；固定止损仍由确定性代码计算，并受轮询和数据源延迟影响。

## 第三阶段收尾口径

完整记录见 `docs/第三阶段收尾记录.md`。

固定止损采用 `close <= stop_loss_price`。收盘价高于人工确认止损价时不触发；等于或低于时触发。回到止损价上方会解除活动事件，之后再次触及会创建新事件并立即提醒。持续触发仍按高优先级独立间隔重复提醒。

固定止损只使用目标时刻已确认分钟线的收盘价和有效人工持仓批次。历史分钟缺口存在时，当前分钟仍可独立判断止损；连续检查点停留在缺口之前，依赖完整序列的 VWAP、收益率、分钟均线、日内高低点、放量突破和相对板块表现保持未知。

目标分钟缺失、未确认、时间不匹配或本轮存在内容冲突等质量异常时，价格线保持状态未知。已有活动事件收到未知检查，不会被误记为“无触发”或解除。

## 第四阶段 A 离线摘要验收

默认演示配置固定使用 `runtime/summary-test.db`，程序还会检查文件名，拒绝把演示持仓写入主库。

```powershell
python -m pandeng.app --config configs/summary-test.yaml show-config
python -m pandeng.app --config configs/summary-test.yaml init
python -m pandeng.app --config configs/summary-test.yaml summary-demo --session-date 2026-01-08
python -m pandeng.app --config configs/summary-test.yaml summary-status
python -m pandeng.app --config configs/summary-test.yaml summary-trace --task-id 1
```

虽然 `configs/summary-test.yaml` 中 Ollama 为关闭状态，`summary-demo` 默认显式注入离线模拟模型响应，用来验收异步链路；它不会偷偷连接本机 Ollama。其他普通分析命令没有这项演示例外。

新鲜独立库的预期结果：

- 先输出高优先级固定止损告警，再创建摘要任务；
- 固定告警 2 条，摘要任务 2 条；
- 模型摘要 1 条，模板回退 1 条，模拟模型调用 2 次；
- 固定止损响应内的 `<think>` 和思考通道文本只进入调用审计；
- VWAP 演示响应故意包含输入外数字 `999`，该校验失败，最终保存固定模板；C4 已将提示词、校验和模板中的 VWAP 统一为四位小数；
- 有限收尾完成为“是”。

演示库已有任务、持仓或分析运行时，再次执行会被拒绝。不要删除数据库绕过保护；需要重跑时，先把旧库移动到新的桌面备份。

## 可选：真实本机 R1 实测

先完成全部离线验收，再决定是否执行本步骤。真实演示使用另一个独立库 `runtime/summary-live-test.db`，只包含模拟行情和模拟持仓。

确认本机 Ollama API 已可用：

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -Method Get
```

如果服务尚未运行，可在另一个 PowerShell 窗口执行 `ollama serve`。已经正常运行时不要重复启动。随后执行：

```powershell
python -m pandeng.app --config configs/summary-live-test.yaml init
python -m pandeng.app --config configs/summary-live-test.yaml summary-demo --session-date 2026-01-08 --real-ollama
python -m pandeng.app --config configs/summary-live-test.yaml summary-status
python -m pandeng.app --config configs/summary-live-test.yaml summary-trace --task-id 1
```

只有显式添加 `--real-ollama` 并使用允许的配置文件时才会请求真实模型。模型不可用、超时或输出未通过校验时，固定告警和模板结果仍保留。真实模型的合格摘要数量不作预设，严格校验导致回退也属于可审计结果。

`summary-trace` 显示应用实测等待毫秒数，以及 Ollama 实际返回的总耗时、加载耗时、生成 token 数和生成耗时；接口未返回的字段显示“未知”。

## 异步与异常处理

摘要只接收 `live` 模式下经过事件合并和提醒频率控制后实际产生的 `RuleNotice`。调用方先输出固定告警，再调用 `SummaryService.submit()`。恢复和回放检查没有模型摘要资格。

- 有界队列满时立即保存固定模板，不排队等待模型；
- 单工作线程保证同时最多一个模型请求；
- 每次数据库操作自行创建并关闭 SQLite 连接，不把连接交给另一线程；
- 任务以事件检查 ID 去重，重复提交不会重复请求模型；
- 上次遗留的 `pending` 或 `running` 任务在下次启动时恢复，已完成任务不重跑；
- 收尾等待有上限，超时任务保存模板，迟到模型结果只留审计，不能覆盖回退结果；
- R1 完整思考段被剥离，未闭合或错位标签会使响应回退；
- 最终摘要只设置长度上限，没有最低字数要求。

输出校验包括股票代码、完整行情时间、关键数值、输入外数字、空文本、异常标签、交易指令和无依据的原因预测。原始结构化告警始终保留。

## 持仓人工确认与减仓更正

```powershell
python -m pandeng.app holdings list
python -m pandeng.app holdings add --code "600000" --quantity 100 --buy-date 2026-01-05 --stop-loss 9.5 --confirmation "我确认添加该真实持仓批次"
python -m pandeng.app holdings t1 --code "600000" --as-of-date 2026-01-06
python -m pandeng.app holdings archive --lot-id 1 --reason "人工核对" --confirmation "我确认归档该持仓批次"
```

新增和归档都会追加 `position_audit`，数量必须是 100 股整数倍。

当前没有 `holdings reduce` 或 `holdings correct`。部分卖出时，先归档原批次并在原因中写明人工核对情况；如仍有剩余研究记录，再新增剩余批次，买入日期沿用核对后的原买入日期，止损价重新确认。录入错误也采用归档后新增。不要直接修改 SQLite。该流程只维护个人记录，不代表账户查询或实际交易。

## T＋1、MA20 与既有调度

T＋1 按有效批次和明确交易日历计算。当日买入可卖数量为 0；买入日后的首个开市日到达后，该批次全部记录数量具备可卖资格。必要日历缺失时状态为未知，已经取得的可卖资格不会因更晚日历缺失而反转。

MA20 只使用 `confirmed=true`、`adjustment="qfq"` 的日 K。拐头只比较相邻 MA20：下降归零，不下降递增。正式信号要求确认日 K 收盘价严格高于 MA20，且拐头天数至少为 3。收盘不等于日 K 已确认，后续确认和回放只能生成带模式的补记。

原有命令仍可使用：

```powershell
python -m pandeng.app simulate --session-date 2026-01-05 --minutes 5 --daily-days 25
python -m pandeng.app --config configs/analysis-test.yaml analysis-status --session-date 2026-01-07
python -m pandeng.app --config configs/monitor-test.yaml monitor-status
```

默认每 300 秒检查一次，每轮增量补齐中间所有一分钟数据。午休停止轮询，收盘有限时收尾后退出。`Ctrl+C` 保存状态和检查点，防重入逻辑继续有效。

## 摘要配置

旧 YAML 没有 `summary` 时使用下列默认值，不必为了升级修改个人配置：

```yaml
summary:
  queue_capacity: 8
  shutdown_timeout_seconds: 10
  max_chars: 400
```

真实模型独立演示配置把收尾上限设为 60 秒，以覆盖首次加载所需的较长等待。个人固定止损价仍只保存在人工确认的持仓批次中。敏感信息通过环境变量管理，`.env.example` 只是模板，程序不会自动加载 `.env`，日志和配置输出不显示真实密钥。

## 远期功能与安全清理

D4 已完成桌面本地复盘、只读数据体检、质量明细和入库批次追溯，数据库结构保持版本 6，默认源码验收完全离线。C3 双探针只收集分钟闭合与更新证据，不自动生成确认边界，真实分钟也尚未接入自动轮询。盘中闭合语义、真实前复权日线和可靠交易日历仍需分别实测，不能把收盘后样本、离线双探针或模拟云端响应推定为真实能力。真实 Ollama 复测、D4 Windows 成品、GitHub 发布和杀毒表现仍待用户在自己的环境中验收。

盘灯不需要管理员权限，不安装驱动，不修改注册表，不设置开机启动。不要手动修改 Ollama 的 `blobs`、`manifests` 等模型文件，不要让杀毒软件排除整个项目目录来规避检查。移动或删除任何数据库前先备份，并确认没有盘灯程序使用它。
