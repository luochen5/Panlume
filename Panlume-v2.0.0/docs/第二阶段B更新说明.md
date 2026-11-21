# 盘灯第二阶段 B 更新与验收说明

## 1. 这次更新做什么

第二阶段 B 在已有模拟行情和 CSV 回放之上，加入低频调度与恢复能力：

- 每轮增量补齐中间所有一分钟数据；
- 每只股票独立保存连续检查点和“状态未知”；
- 暂停后继续时先补数，恢复记录明确标为离线补跑；
- 断网恢复后先检查完整性，再开放逐股票分析门控；
- 午休不轮询，下午续接上午检查点；
- 11:30 与 15:00 处理时段边界，收盘收尾有等待上限；
- 日 K 未确认时，正式日线结论保持未知；
- `Ctrl+C` 保存停止状态，上一轮未结束时防止重入。

本阶段仍不计算指标和规则，不连接真实行情，不调用 Ollama 或云端，不修改真实持仓。你已具备 AI 测试条件，这部分留到第四阶段实测。

## 2. 更新包不会动什么

压缩包不包含以下内容：

- `.venv`；
- `runtime` 目录和任何 `.db`、`.db-wal`、`.db-shm`；
- 你的 `.env` 或真实密钥；
- 桌面旧备份；
- Ollama 的模型文件。

下面的复制命令只更新程序、测试、文档和依赖清单，不覆盖 `configs/settings.yaml`。数据库升级只新增表并保留原记录，测试中已经验证一条用户确认的持仓批次升级后不变。

## 3. 解压到一个新的临时目录

打开普通 PowerShell，无需管理员。假设压缩包已经下载到桌面：

```powershell
$Zip = "$HOME\Desktop\盘灯-第二阶段B.zip"
$Package = "$HOME\Desktop\pandeng-stage2b-$(Get-Date -Format yyyyMMdd-HHmmss)"

Test-Path $Zip
New-Item -ItemType Directory -Path $Package | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $Package
Test-Path "$Package\pandeng\app\cli.py"
```

预期：

- `Test-Path $Zip` 返回 `True`；
- 最后一条也返回 `True`。

如果第一条是 `False`，先找到实际下载位置，不要继续复制。也可以右键压缩包，选择“全部解压”，再把 `$Package` 改成实际解压目录。

## 4. 启用原虚拟环境并备份

```powershell
Set-Location D:\pandeng
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1

python --version
python -c "import yaml; print('PyYAML 可用')"
```

确认 Python 来自原虚拟环境：

```powershell
python -c "import sys; print(sys.executable)"
```

预期路径以 `D:\pandeng\.venv\Scripts\python.exe` 结尾。

备份源码、配置、测试和主数据库，不复制庞大的 `.venv`：

```powershell
$Backup = "$HOME\Desktop\pandeng-before-stage2b-$(Get-Date -Format yyyyMMdd-HHmmss)"
New-Item -ItemType Directory -Path $Backup | Out-Null
Copy-Item "D:\pandeng\pandeng" $Backup -Recurse
Copy-Item "D:\pandeng\configs" $Backup -Recurse
Copy-Item "D:\pandeng\tests" $Backup -Recurse

if (Test-Path "D:\pandeng\runtime\pandeng.db") {
    Copy-Item "D:\pandeng\runtime\pandeng.db" $Backup
}
if (Test-Path "D:\pandeng\runtime\pandeng.db-wal") {
    Copy-Item "D:\pandeng\runtime\pandeng.db-wal" $Backup
}

Write-Host "备份位置：$Backup"
```

备份只新增文件，不删除桌面旧备份。执行更新前关闭正在访问盘灯数据库的终端命令。

## 5. 复制第二阶段 B 文件

仍保持 `$Package` 指向刚解压的目录：

```powershell
Copy-Item -Path "$Package\pandeng\*" -Destination "D:\pandeng\pandeng" -Recurse -Force
Copy-Item -Path "$Package\tests\*" -Destination "D:\pandeng\tests" -Recurse -Force
Copy-Item -Path "$Package\docs\*" -Destination "D:\pandeng\docs" -Recurse -Force
Copy-Item -Path "$Package\examples\*" -Destination "D:\pandeng\examples" -Recurse -Force
Copy-Item -Path "$Package\README.md", "$Package\requirements.txt", "$Package\requirements-dev.txt" -Destination "D:\pandeng" -Force
```

这里使用 `pandeng\*`、`tests\*` 的内容复制方式，避免多套一层目录。不要复制整个解压目录到 `D:\pandeng` 内部。

检查新命令已经出现：

```powershell
python -m pandeng.app --help
```

输出中应包含：

```text
monitor-demo
monitor-status
```

## 6. 先运行自动化测试

```powershell
Set-Location D:\pandeng
python -m pytest -q
```

预期：

```text
47 passed
```

如果这里失败，先暂停后续演示，保留完整报错。测试只使用临时目录，不会清空 `runtime\pandeng.db`。

再升级主数据库结构：

```powershell
python -m pandeng.app init
python -m pandeng.app status
```

预期显示“初始化/升级完成”和“数据库结构版本：3”。这一步不会运行调度演示，也不会请求真实行情。

## 7. 建立独立调度演示配置

以下命令从现有配置复制一份，只修改数据库路径。若目标文件已经存在，命令会保留它，不覆盖：

```powershell
$Source = "D:\pandeng\configs\settings.yaml"
$Target = "D:\pandeng\configs\monitor-test.yaml"

if (Test-Path $Target) {
    Write-Host "已存在，不覆盖：$Target"
} else {
    $Lines = Get-Content $Source
    $Updated = foreach ($Line in $Lines) {
        if ($Line -match '^\s{2}database:\s*') {
            '  database: runtime/monitor-test.db'
        } else {
            $Line
        }
    }
    [System.IO.File]::WriteAllLines(
        $Target,
        $Updated,
        [System.Text.UTF8Encoding]::new($false)
    )
    Write-Host "已创建：$Target"
}
```

检查配置：

```powershell
Set-Location D:\pandeng
python -m pandeng.app --config configs/monitor-test.yaml show-config
```

应看到数据库指向 `D:\pandeng\runtime\monitor-test.db`。旧配置没有 `fetch_timeout_seconds` 和 `close_timeout_seconds` 也可以，程序分别使用默认值 20 和 45。

初始化独立数据库：

```powershell
python -m pandeng.app --config configs/monitor-test.yaml init
```

## 8. 一键验收暂停、断网、午休和收盘

```powershell
python -m pandeng.app --config configs/monitor-test.yaml monitor-demo --session-date 2026-01-06
```

该命令几秒内完成，不会等待真实时间。按当前 5 只示例自选股，关键输出应包括：

```text
1. 09:35 首轮增量：新增 25；可分析代码 5
2. 暂停期间：新增请求 0 次
3. 09:41 继续并补跑：新增 30；离线恢复=是
4. 09:46 注入断网：unknown；新增 0；可分析代码 0
5. 09:51 网络恢复并补数：新增 50；离线恢复=是
6. 午休：新增请求 0 次
7. 13:01 续接上午检查点
8. 15:00 最后一轮
9. 日 K 状态=unknown；已停止新轮询并自动退出
未访问真实行情、未调用 Ollama 或云端、未修改真实持仓
```

演示日 K 故意标记为未确认。即使分钟行情完整，正式日线状态仍应是 `unknown`。

查看保存的检查点与事件：

```powershell
python -m pandeng.app --config configs/monitor-test.yaml monitor-status
```

预期看到：

- 运行状态 `closed`；
- 停止原因 `session_closed`；
- 日 K `unknown`；
- 每只股票连续到 `2026-01-06T15:00:00+08:00`；
- 最近事件包含 `daily_unconfirmed`、`monitor_closed` 和 `offline_recovery`。

同一日期再次运行会被拒绝，以免覆盖历史。需要再演示时换新日期：

```powershell
python -m pandeng.app --config configs/monitor-test.yaml monitor-demo --session-date 2026-01-07
```

不需要删除旧数据库。

## 9. 本次自动化验收覆盖什么

47 项测试包含第一、第二阶段的全部回归，并新增以下场景：

| 验收点 | 验证方式 |
|---|---|
| 增量轮询 | 第二轮只取检查点之后的一分钟，不重复保存 |
| 暂停恢复 | 暂停期间零请求；继续后补全暂停区间 |
| 离线恢复标记 | 每只股票写入 `offline_recovery`，明确只能补记 |
| 断网 | 受影响代码状态未知，分析门控不通过 |
| 网络恢复 | 先补齐并检查完整性，再允许分析 |
| 恢复仍有缺口 | 连续检查点停在缺口前，下轮继续补 |
| 单股异常 | 健康股票仍可分析，异常股票独立保持未知 |
| 午休 | 12:00 零请求；13:01 可回补上午缺口 |
| 收盘 | 最后一轮到 15:00，随后自动退出 |
| 收盘断网 | 记录失败原因后退出，不无限重试 |
| 日 K 未确认 | 保存 `confirmed=0`，正式日线状态未知 |
| 收盘超时 | 使用慢速模拟数据源验证有限等待 |
| `Ctrl+C` | 等待期间和轮询途中都保存停止状态 |
| 防重入 | 上一轮活动时不新增嵌套轮次 |
| 完整交易日 | 模拟时钟无实际等待跑完 240 分钟／股票 |
| 非交易日 | 日历确认不开市后不安排分钟轮询 |
| 收盘后启动 | 不补做自动轮询，仍可手动查询和回放 |
| 旧库升级 | 结构版本 2 升到 3，已确认持仓批次不变 |
| 命令行 | 演示、状态查询和同日防覆盖均实际执行 |

## 10. 给第三阶段留的接口

新增表：

- `monitor_state`：当前交易日、暂停／停止／收盘状态和日 K 状态；
- `monitor_code_states`：逐股票连续检查点、最近尝试时间和未知原因；
- `monitor_rounds`：轮次触发原因、恢复标记、数量与可分析代码数；
- `monitor_events`：暂停、断网、恢复、日 K 未确认和退出等追溯事件。

`MonitorService.run()` 已在模拟时钟下完整运行到收盘。`monitor-demo` 则按关键时间点逐步演示。真实行情服务接入后继续使用同一适配器接口。

第三阶段只能处理 `RoundResult.analysis_ready_codes` 中的代码。若 `RoundResult.recovery=True`，止损等条件只能写成离线恢复补记，不能显示成当时已经实时提醒。原始分钟表不增加伪造的实时标记。

数据源超时只中断等待；迟到的只读结果会被丢弃，工作线程不写 SQLite。正在执行的数据库事务会正常提交或回滚，不用强制终止方式破坏数据库。

## 11. 常见报错

### 没有 `monitor-demo` 命令

新程序未复制完整，或启动位置不对。检查：

```powershell
Set-Location D:\pandeng
Test-Path D:\pandeng\pandeng\monitor\service.py
Test-Path D:\pandeng\pandeng\app\cli.py
python -m pandeng.app --help
```

不要直接删除目录。若发现多套一层，先把异常目录移动到新的桌面备份，再按第 5 节复制内容。

### `no such table: monitor_state`

当前配置指向的数据库尚未升级。对同一个配置文件执行：

```powershell
python -m pandeng.app --config configs/monitor-test.yaml init
```

主数据库则使用默认命令 `python -m pandeng.app init`。

### `No module named 'yaml'`

通常是虚拟环境未启用。重新执行第 4 节，并确认 `sys.executable` 指向 `D:\pandeng\.venv`。

### 虚拟环境脚本被禁止

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
```

只影响当前 PowerShell，不需要管理员，也不修改系统长期策略。

### 提示该日期已有调度验收记录

这是防覆盖保护。换一个新的 `--session-date`，不要删除追溯表。

### 测试不是 47 项或通过数不符

先检查是否混用了旧测试和新程序：

```powershell
Test-Path D:\pandeng\tests\test_monitor.py
Test-Path D:\pandeng\tests\test_monitor_cli.py
Test-Path D:\pandeng\pandeng\tests
```

前两个应为 `True`，最后一个通常应为 `False`。若仍失败，保留完整输出并暂停，不反复复制或重置数据库。

## 12. 本阶段验收清单

- [ ] 原虚拟环境能够启用，PyYAML 可用；
- [ ] 新命令出现在帮助中；
- [ ] `47 passed`；
- [ ] 主数据库升级到结构版本 3，旧数据未清空；
- [ ] 调度演示使用独立 `monitor-test.db`；
- [ ] 暂停和午休都显示新增请求 0；
- [ ] 断网时可分析代码为 0；
- [ ] 恢复轮次明确标为离线恢复；
- [ ] 下午从上午检查点续接；
- [ ] 收盘后状态为 `closed`；
- [ ] 未确认日 K 保持 `unknown`；
- [ ] 没有真实行情、真实持仓或模型调用。

完成这些后，再进入第三阶段的指标、规则、固定止损、T+1 与 MA20。第四阶段再利用现在具备的供电和环境条件，对 Ollama 做异步摘要与资源实测。
