# 盘灯真实行情适配器 B 更新说明

> 历史版本说明：B 阶段的 159 项测试和独立 `adapter-test.db` 验收仍保留。当前安装、Tushare 虚拟响应映射及 214 项测试以 `真实行情接入C1更新说明.md` 为准；已经运行过 B 演示的用户无需重跑。

本包以第四阶段 A 的完整源码为基础，已经包含真实行情适配器预备阶段的全部代码，并新增只读数据体检、1／5／10 分钟轮询档位提示和对应自动化测试。尚未选择或连接任何真实数据服务商，未新增运行依赖，数据库结构继续保持版本 5。

项目只用于公开信息处理、个人研究记录和软件学习，不连接外部账户，不执行外部操作。本包不会自动下单，不会修改真实持仓，也不会主动请求真实行情、真实 Ollama 或云端。真实持仓只能由用户通过专用命令显式确认更新，本地模型只整理已筛选事件的文字，不能修改原始数据和程序结论。敏感配置继续通过环境变量管理，`.env.example` 仅为示例。

## 一、这次完成了什么

1. 新增 `data-audit`，按交易日和明确的回看时刻检查日历、分钟缺口、未确认记录、质量错误、价格线、连续检查点和当日前复权日 K。
2. 体检连接启用 `PRAGMA query_only = ON`，不补数、不修复冲突、不计算规则、不请求行情、不调用模型。
3. 历史分钟缺口与有效目标分钟的价格线分开报告，状态可为 `ok`、`price_only` 或 `unknown`。
4. 日 K 独立报告 `confirmed`、`unconfirmed`、`missing` 或 `unknown`，不会因日 K 状态而重标完整分钟。
5. 分钟质量错误只统计到 `as-of-time`，更晚发生的错误不会污染较早时刻的回看。
6. `show-config` 和 `status` 显示 60、300、600 秒推荐档位，其他 60～3600 秒合法整数仍按自定义档处理。
7. `adapter-demo` 在两轮增量验收后自动执行一次只读体检；既有演示库无需重跑，可直接使用 `data-audit`。
8. 全量离线测试由预备阶段的 137 项增加到 **159 项**。

本次没有修改止损、T＋1、MA20、指标、规则、摘要、持仓或数据库表结构。旧个人 YAML 不需要增加新键。

## 二、B 阶段相关文件结构

```text
pandeng/
├─ configs/
│  ├─ settings.yaml               # 默认 300 秒；注释列出推荐轮询档位
│  └─ adapter-test.yaml           # 独立离线演示配置，Ollama 和云端均关闭
├─ examples/
│  └─ market_feed.json            # 两只虚拟对象、12 条分钟、2 条日线、1 条日历
├─ pandeng/
│  ├─ app/
│  │  └─ cli.py                   # data-audit、体检输出和轮询档位显示
│  ├─ config/
│  │  └─ schema.py                # POLLING_PRESETS 与档位标签
│  └─ data/
│     ├─ audit.py                 # 只读数据体检
│     ├─ json_feed.py             # 严格 JSON 文件适配器
│     ├─ response.py              # 分钟、日线、日历响应范围防护
│     └─ adapter_demo.py          # 两轮增量验收与只读体检汇总
├─ tests/
│  ├─ test_data_audit.py          # 体检核心口径与只读性
│  ├─ test_data_audit_cli.py      # 命令行、轮询标签和零写入验证
│  ├─ test_json_feed.py
│  ├─ test_adapter_scope.py
│  └─ test_adapter_demo_cli.py
└─ docs/
   ├─ 真实行情适配器B更新说明.md
   └─ 真实行情适配器预备阶段更新说明.md  # 历史版本记录
```

更新包包含运行所需的完整源码和测试，不包含 `.venv`、`runtime`、数据库、缓存、`.pyc`、`.git`、密钥或 Ollama 模型。

## 三、数据流与体检位置

当前离线验收的数据流：

```text
本地 JSON 样例
  → MarketDataAdapter 三个只读接口
  → 请求代码与时间／日期范围检查
  → 既有清洗、去重、冲突检查和 SQLite 入库
  → 既有逐股票连续检查点与缺口门控
  → B 阶段只读数据体检
```

体检位于入库和调度之后，只汇总当前数据库已经保存的事实。它不会把缺口补成完整，也不会把未知改成“无触发”。当日分钟记录完整时，日 K 仍需单独核对确认状态。

以后选定合规数据服务时，新增真实适配器，把服务商字段映射为既有 `MinuteBar`、`DailyBar` 和 `TradingDay`。指标、规则和模型层不需要直接理解服务商原始字段。

## 四、本机更新前的顺序

本说明适用于已经在 `D:\pandeng` 完成第四阶段 A 的环境，也适用于已经安装适配器预备版的环境。B 包包含预备版代码，直接从第四阶段 A 更新即可，不必先安装预备版。

如果本机仍停留在第三阶段，先按 `docs/第四阶段A更新说明.md` 完成第四阶段 A 和版本 5 副本升级验收，再使用本包。不要把 B 包直接覆盖到第三阶段源码上。

关闭正在运行的盘灯程序，不要在写数据库时复制文件。以下命令默认 ZIP 已下载到桌面；只复制代码块，不要复制 PowerShell 提示符或预期输出。

```powershell
$Desktop = [Environment]::GetFolderPath("Desktop")
$Project = "D:\pandeng"
$Zip = Join-Path $Desktop "盘灯-真实行情适配器B.zip"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Extract = Join-Path $Desktop "pandeng-adapter-b-$Stamp"
$Backup = Join-Path $Desktop "pandeng-before-adapter-b-$Stamp"

Test-Path $Zip
Test-Path $Project\pandeng\summarizer\worker.py
New-Item -ItemType Directory -Path $Extract -Force | Out-Null
New-Item -ItemType Directory -Path $Backup -Force | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $Extract

Test-Path $Extract\pandeng\data\audit.py
Test-Path $Extract\pandeng\data\json_feed.py
Test-Path $Extract\pandeng\data\response.py
Test-Path $Extract\configs\adapter-test.yaml
Test-Path $Extract\examples\market_feed.json
Test-Path $Extract\tests\test_data_audit.py
Test-Path $Extract\tests\test_data_audit_cli.py
Test-Path $Extract\docs\真实行情适配器B更新说明.md
```

路径检查应全部为 `True`。如果 ZIP 路径为 `False`，先核对浏览器下载位置和完整文件名，包括是否出现 `(1)`，不要继续复制。

## 五、备份并复制更新

以下备份保留代码、个人配置、样例和整个 `runtime`，不复制 `.venv`：

```powershell
foreach ($Name in @("pandeng", "configs", "docs", "tests", "examples", "runtime")) {
    $Source = Join-Path $Project $Name
    if (Test-Path $Source) {
        Copy-Item -LiteralPath $Source -Destination $Backup -Recurse -Force
    }
}
foreach ($Name in @("README.md", "requirements.txt", "requirements-dev.txt", ".env.example", ".gitignore")) {
    $Source = Join-Path $Project $Name
    if (Test-Path $Source) {
        Copy-Item -LiteralPath $Source -Destination $Backup -Force
    }
}
Write-Host "更新前备份位于：$Backup"
```

复制完整源码、测试和文档，同时只更新专用的适配器演示配置与 JSON 样例：

```powershell
Copy-Item -Path "$Extract\pandeng\*" -Destination "$Project\pandeng" -Recurse -Force
Copy-Item -Path "$Extract\tests\*" -Destination "$Project\tests" -Recurse -Force
Copy-Item -Path "$Extract\docs\*" -Destination "$Project\docs" -Recurse -Force
Copy-Item -LiteralPath "$Extract\README.md", "$Extract\requirements.txt", "$Extract\requirements-dev.txt", "$Extract\.env.example", "$Extract\.gitignore" -Destination $Project -Force

New-Item -ItemType Directory -Path "$Project\examples" -Force | Out-Null
Copy-Item -LiteralPath "$Extract\examples\market_feed.json" -Destination "$Project\examples\market_feed.json" -Force
Copy-Item -LiteralPath "$Extract\configs\adapter-test.yaml" -Destination "$Project\configs\adapter-test.yaml" -Force

Test-Path $Project\pandeng\data\audit.py
Test-Path $Project\pandeng\data\json_feed.py
Test-Path $Project\configs\adapter-test.yaml
Test-Path $Project\examples\market_feed.json
Test-Path $Project\tests\test_data_audit.py
Test-Path $Project\docs\真实行情适配器B更新说明.md
```

这里没有复制压缩包中的 `settings.yaml`、`analysis-test.yaml`、`summary-test.yaml` 或 `summary-live-test.yaml`，原个人 YAML 继续保留。主数据库、既有演示库、真实持仓和桌面备份均不清空。

B 阶段没有数据库结构变更，不需要为了本次更新再次升级主库。可用只读状态命令核对：

```powershell
Set-Location D:\pandeng
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pandeng.app status
```

数据库结构版本应继续为 `5`。如果仍显示 `4`，说明第四阶段 A 的数据库升级尚未完成，先回到该阶段说明处理，不要直接运行适配器演示。

## 六、运行全量离线测试

```powershell
Set-Location D:\pandeng
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python --version
python -m pytest -q
```

预期最后一行：

```text
159 passed
```

测试使用临时数据库、模拟时钟、本地 JSON、模拟模型响应和本机临时 HTTP 服务，不请求真实行情、真实 Ollama 或云端。

若提示缺少 `pytest`：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

若提示 `No module named yaml`，通常是当前终端没有启用 `D:\pandeng\.venv`。重新执行激活命令即可，不需要管理员权限。

如果测试数仍为 121 或 137，先核对当前目录和上述文件检查，不要删除旧测试或数据库来凑结果。常见原因是解压目录多套一层，或复制到了另一个项目目录。

## 七、从未运行过适配器演示：完整离线验收

仅当 `runtime/adapter-test.db` 尚未运行过演示时执行本节。先检查配置，数据库应指向 `runtime/adapter-test.db`，轮询为 60 秒的 1 分钟档，Ollama 和云端均应关闭：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml show-config
python -m pandeng.app --config configs/adapter-test.yaml init
python -m pandeng.app --config configs/adapter-test.yaml adapter-demo --session-date 2026-01-12
python -m pandeng.app --config configs/adapter-test.yaml monitor-status
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00
```

新鲜独立库的关键预期输出：

```text
1. 09:31～09:34 首轮：状态 ok；收到 8；新增 8；可分析 2 只。
2. 09:35～09:36 增量：状态 ok；收到 4；新增 4；可分析 2 只。
3. 当日明确前复权日 K：收到 2；新增 2；状态 ok。
5. 只读数据体检：分钟总体 ok；完整 2 只；仅价格线可用 0 只；状态未知 0 只。
验收记录汇总：分钟 12 条；前复权日线 2 条；交易日历 1 条；只读分钟请求 4 次。
```

四段分钟请求应依次覆盖：

```text
600000：09:31 至 09:34
000001：09:31 至 09:34
600000：09:35 至 09:36
000001：09:35 至 09:36
```

两只股票的连续检查点都应到 `2026-01-12T09:36:00+08:00`。最后的独立体检应显示：

```text
分钟总体状态 ok；完整 2 只；仅价格线可用 0 只；状态未知 0 只。
分钟已保存 6/6
当日前复权日 K 已确认
本次体检只读 SQLite，没有补数、改写结论、请求行情或调用模型。
```

演示中的日线入库只验证前复权接口和明确确认字段，不代表自动完成收盘日 K 确认流程。

## 八、已经运行过预备版演示：只执行只读体检

已有 `adapter-test.db` 时，不必重跑 `adapter-demo`，也不要删除、移动或清空旧测试库。更新代码并通过 159 项测试后，直接执行：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml monitor-status
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00
```

既有 12 条分钟、2 条日线、1 条日历和连续检查点应保持不变。`data-audit` 不会新增处理批次、质量记录、运行日志或调度轮次。

## 九、只读体检参数与状态解释

`data-audit` 要求显式提供两个时间参数：

- `--session-date`：交易日，格式为 `YYYY-MM-DD`；
- `--as-of-time`：同一日期的交易整分钟，必须带时区，例如 `2026-01-12T09:36:00+08:00`。

可检查全部观察对象，也可用 `--code` 限定为当前 YAML 中的一只：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml data-audit --session-date 2026-01-12 --as-of-time 2026-01-12T09:36:00+08:00 --code "600000"
```

`as-of-time` 只接受 09:31～11:30、13:01～15:00 的整分钟。午休、带秒时间、无时区时间和跨日期时间会被拒绝。程序不会用当前电脑时间替代历史回看时刻。

分钟状态：

| 状态 | 含义 | 价格线 |
| --- | --- | --- |
| `ok` | 日历明确开市，截至目标分钟的应有记录完整、全部确认，区间内没有分钟质量错误 | 可用 |
| `price_only` | 历史序列存在缺口、未确认记录或质量错误，但目标分钟已保存、已确认且没有目标分钟质量冲突 | 可用，完整上下文仍需另行判断 |
| `unknown` | 目标分钟缺失、未确认或有质量冲突，无法开放价格线 | 未知 |

交易日历明确休市时，价格线不开放。日历缺失或有冲突时会明确说明原因；程序不会凭星期几猜测开市状态。

日 K 状态单独显示：

| 状态 | 含义 |
| --- | --- |
| `confirmed` | 当日明确前复权日 K 已保存且确认 |
| `unconfirmed` | 当日明确前复权日 K 已保存，但尚未确认 |
| `missing` | 当日明确前复权日 K 尚未保存 |
| `unknown` | 当日日线存在质量冲突，不能给出正常状态 |

日 K 缺失、未确认或冲突不会把完整分钟重新标成不完整。分钟体检通过也不会把收盘或日 K 确认混为一谈。

检查点用于展示调度进度。CSV 入库数据没有调度检查点时，仍可依据实际分钟记录完成体检。`data-audit` 也不会借检查点把缺失分钟补成连续。

命令返回 0 表示体检顺利执行，不表示数据健康。发现缺口、未确认或冲突时，报告会显示相应状态，命令本身仍可返回 0。只有参数、配置或数据库访问失败时才返回错误。

本报告按当前数据库回看截至时刻应有的数据，不证明这些数据在当时已经实时到达。分钟质量错误只统计到 `as-of-time`，更晚发生的错误不影响较早时刻的回看结果。

## 十、1／5／10 分钟轮询档位

轮询仍只使用现有配置键 `monitor.interval_seconds`。B 阶段没有新增 YAML 字段，也没有增加会与 YAML 冲突的命令行参数。

在自己的 `configs/settings.yaml` 中保留以下结构，按需修改数值：

```yaml
monitor:
  # 60=1 分钟，300=5 分钟（默认），600=10 分钟。
  # 其他 60～3600 秒的整数按自定义档处理。
  interval_seconds: 300
  fetch_timeout_seconds: 20
  close_timeout_seconds: 45
```

档位特点：

- **60 秒：**1 分钟档，检查更及时，数据请求更频繁；
- **300 秒：**5 分钟默认档，继续作为默认平衡选择；
- **600 秒：**10 分钟档，请求更少，固定止损提醒可能额外延迟；
- **其他合法整数：**自定义档，例如 120 秒显示为 2 分钟，90 秒显示为 1.5 分钟。

修改后检查：

```powershell
python -m pandeng.app show-config
python -m pandeng.app status
```

`show-config` 会列出全部推荐档位，`status` 会显示当前实际档位。无论间隔如何，每轮都会增量补齐中间所有一分钟数据，不会只取最新一条。轮询间隔不增加普通模型调用，固定止损仍由确定性 Python 计算，并受轮询和数据源延迟影响。

## 十一、常见问题与防重跑

### 提示“数据库尚未初始化”

先核对命令中的 `--config` 和 `show-config` 显示的数据库路径。独立演示使用：

```powershell
python -m pandeng.app --config configs/adapter-test.yaml init
```

主库是否初始化或升级应按其自身阶段处理，不要为了运行演示而改指向主库。

### 提示“只允许写入文件名 adapter-test.db”

这是主库隔离保护。检查是否忘记：

```powershell
--config configs/adapter-test.yaml
```

不要为了通过检查而重命名主库。

### 提示“只使用全新的 adapter-test.db”

这是防重跑保护，说明演示库已有业务数据。直接执行第八节的 `monitor-status` 和 `data-audit` 即可完成 B 阶段验收，无需重跑演示。

确需从头重验时，先确认所有盘灯程序已经正常退出，再把主文件和可能存在的 WAL、SHM 文件一起移动到本次备份。以下命令沿用第四节的 `$Project`、`$Backup` 和 `$Stamp`：

```powershell
foreach ($Suffix in @("", "-wal", "-shm")) {
    $OldAdapter = Join-Path $Project "runtime\adapter-test.db$Suffix"
    if (Test-Path $OldAdapter) {
        Move-Item -LiteralPath $OldAdapter -Destination (Join-Path $Backup "adapter-test-$Stamp.db$Suffix")
    }
}
python -m pandeng.app --config configs/adapter-test.yaml init
python -m pandeng.app --config configs/adapter-test.yaml adapter-demo --session-date 2026-01-12
```

不要手工拼接或修改 SQLite 内容，也不要删除数据库绕过保护。

### 提示“--code 必须属于当前 YAML 的观察对象列表”

体检只允许检查当前配置中的代码。先运行 `show-config` 核对自选股；如果要检查另一只股票，先由你手动修改对应 YAML，不能用命令参数绕过观察列表。

### 提示时间必须带时区、必须是整分钟或日期不一致

使用完整格式，例如：

```text
2026-01-12T09:36:00+08:00
```

`2026-01-12 09:36:00`、`2026-01-12T09:36:30+08:00`、午休时间和与 `session-date` 不同日期的时间都会被拒绝。

### 报告显示 `price_only` 或 `unknown`，但命令返回 0

这是预期行为。体检成功执行与数据状态分开表达。保留报告和质量审计，先核对数据源、请求区间与确认状态，不要把未知手工改成“无触发”。

### PowerShell 出现 `>>` 或大量红色文字

`>>` 通常表示引号或括号没有闭合，可按 `Ctrl+C` 返回后重新复制完整命令。只复制代码块，不要复制 `PS D:\pandeng>`、旧输出或预期结果。不要关闭杀毒软件，也不要通过大范围排除目录规避检查。

## 十二、真实服务商与银河证券的后续边界

当前 B 阶段仍未实现真实联网适配器，也没有创建或写入 `real-data-test.db`。后续接入前仍需核对：

- 服务条款允许个人本地研究、保存方式和使用频率；
- 分钟线、前复权日线和交易日历具有正式接口及字段文档；
- 时区、分钟点归属、成交量单位、复权方式和确认状态经过实测；
- 历史深度足够补齐断网期间的缺口；
- 凭证只从环境变量读取，不进入 YAML、源码、日志或数据库；
- 请求具有超时、频率限制、有限重试和响应范围检查；
- 先用少量股票和独立 `real-data-test.db` 验证，是否接入主库由你另行明确确认。

银河证券可以保留为未来候选。拥有银河证券账户不等于已经拥有行情 API 权限。只有当时正式面向个人账户提供符合要求的授权行情 API，并核实权限、费用、服务条款和本地保存条件后，才进入适配器设计与独立测试。

银河证券方向只考虑正式授权行情 API，不读取账户资产或交易信息，不登录交易功能，不查询或下达订单。项目不会索取账号、密码或交易凭证，也不使用屏幕识别、模拟点击、破解客户端或未授权接口。

## 十三、B 阶段验收清单

- [ ] 更新前已备份代码、个人 YAML 和整个 `runtime`，未复制或重建 `.venv`；
- [ ] 新文件检查全部为 `True`，当前目录为 `D:\pandeng`；
- [ ] 全量离线测试显示 `159 passed`；
- [ ] 主库结构版本继续保持 `5`，真实持仓批次数未因更新改变；
- [ ] `show-config` 能显示 60、300、600 秒推荐档位；
- [ ] 新鲜演示库完成两轮增量、12 条分钟、2 条日线、1 条日历和只读体检；已有演示库则直接体检且未重跑；
- [ ] `data-audit` 能区分分钟完整、仅价格线可用和未知，日 K 状态独立显示；
- [ ] 体检没有新增处理批次、质量记录、运行日志或调度轮次；
- [ ] 未请求真实行情、真实 Ollama 或云端，未修改真实持仓；
- [ ] 真实服务商与银河证券仍只停留在后续候选和正式授权核对范围。

## 十四、本次验收边界

B 阶段完成真实行情接入前的通用离线契约、范围防护、增量演示、只读数据体检和轮询档位提示。真实服务商选型与联网接入、云端门控、手动复盘和 MVP 整体验收仍待后续核心阶段。

桌面窗口、Logo、EXE、安全更新、备份恢复、一键卸载、GitHub 发布和杀毒误报验证继续列入远期工程化功能。`Panlume` 仍是未来 EXE／Logo 阶段的候选品牌名，当前目录和 Python 包继续叫 `pandeng`。
