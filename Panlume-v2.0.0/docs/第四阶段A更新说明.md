# 盘灯第四阶段 A 更新说明

本说明适用于已经在 `D:\pandeng` 完成第三阶段验收的 Windows 环境。更新包含第三阶段止损收尾和第四阶段 A 异步摘要。全过程不需要管理员权限，不连接真实行情，不修改 Ollama 模型文件。

## 一、更新包内容

- 固定止损改为 `close <= stop_loss_price`；
- 历史分钟缺口与有效当前分钟止损解耦；
- 新增结构版本 5 和摘要任务、结果、模型调用表；
- 新增 R1 思考段分离、输出校验、固定模板、异常恢复和有界队列；
- 新增两个独立摘要演示配置及完整自动化测试；
- 新增 `docs/第三阶段收尾记录.md`。

更新包不包含 `.venv`、运行数据库、缓存、密钥或 Ollama 模型。

## 二、关闭程序并准备目录

先让正在运行的盘灯演示或监测正常结束，不要在写数据库时复制文件。打开新的普通 PowerShell 窗口，只复制下面代码块中的命令：

```powershell
$Desktop = [Environment]::GetFolderPath("Desktop")
$Project = "D:\pandeng"
$Zip = Join-Path $Desktop "盘灯-第四阶段A.zip"
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$Extract = Join-Path $Desktop "pandeng-stage4a-$Stamp"
$Backup = Join-Path $Desktop "pandeng-before-stage4a-$Stamp"

Test-Path $Zip
Test-Path $Project\pandeng\app\cli.py
New-Item -ItemType Directory -Path $Extract -Force | Out-Null
New-Item -ItemType Directory -Path $Backup -Force | Out-Null
Expand-Archive -LiteralPath $Zip -DestinationPath $Extract

Test-Path $Extract\pandeng\app\cli.py
Test-Path $Extract\pandeng\summarizer\worker.py
Test-Path $Extract\configs\summary-test.yaml
Test-Path $Extract\tests\test_summary_worker.py
```

前两项用于确认 ZIP 和原项目存在，最后四项应全部为 `True`。如果 ZIP 路径为 `False`，先确认浏览器是否把文件放在其他下载目录，以及文件名是否被改成带 `(1)` 的形式；不要继续复制。

## 三、备份代码、个人配置和数据库

以下备份保留 `runtime` 整个目录，不复制 `.venv`：

```powershell
foreach ($Name in @("pandeng", "configs", "docs", "tests", "runtime")) {
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

备份目录应能看到原 `configs` 和 `runtime`。本次操作没有删除、重置或清空任何数据库。

## 四、复制更新，保留个人 YAML

```powershell
Copy-Item -Path "$Extract\pandeng\*" -Destination "$Project\pandeng" -Recurse -Force
Copy-Item -Path "$Extract\tests\*" -Destination "$Project\tests" -Recurse -Force
Copy-Item -Path "$Extract\docs\*" -Destination "$Project\docs" -Recurse -Force
Copy-Item -LiteralPath "$Extract\README.md", "$Extract\requirements.txt", "$Extract\requirements-dev.txt", "$Extract\.env.example", "$Extract\.gitignore" -Destination $Project -Force

Copy-Item -LiteralPath "$Extract\configs\summary-test.yaml" -Destination "$Project\configs\summary-test.yaml" -Force
Copy-Item -LiteralPath "$Extract\configs\summary-live-test.yaml" -Destination "$Project\configs\summary-live-test.yaml" -Force

Test-Path $Project\pandeng\summarizer\worker.py
Test-Path $Project\pandeng\summarizer\demo.py
Test-Path $Project\docs\第三阶段收尾记录.md
Test-Path $Project\configs\summary-test.yaml
```

这里没有复制压缩包中的 `settings.yaml` 和 `analysis-test.yaml`，原个人配置继续有效。旧配置没有 `summary` 节点时会使用程序默认值。

## 五、启用原虚拟环境并运行全量测试

```powershell
Set-Location D:\pandeng
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python --version
python -m pytest -q
```

预期最后一行：

```text
121 passed
```

测试不需要启动 Ollama。若出现 `No module named pytest`，执行：

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

若出现 `No module named yaml`，通常表示当前没有启用原虚拟环境，重新执行上面的激活命令。

## 六、升级已有数据库并核对主库

```powershell
python -m pandeng.app init
python -m pandeng.app status
```

预期：

- 数据库结构版本为 `5`；
- 数据库路径仍为 `D:\pandeng\runtime\pandeng.db`；
- 原真实持仓批次数保持不变；
- 云端开关保持关闭；
- `status` 本身不启动轮询或调用模型。

检查第三阶段既有结果，不要重复运行 `analysis-demo`：

```powershell
python -m pandeng.app --config configs/analysis-test.yaml init
python -m pandeng.app --config configs/analysis-test.yaml analysis-status --session-date 2026-01-07
```

原 `analysis-test.db` 会保留既有演示数据并升级到版本 5。恢复检查的实时提醒仍应为 0。

## 七、运行第四阶段 A 离线演示

```powershell
python -m pandeng.app --config configs/summary-test.yaml show-config
python -m pandeng.app --config configs/summary-test.yaml init
python -m pandeng.app --config configs/summary-test.yaml summary-demo --session-date 2026-01-08
python -m pandeng.app --config configs/summary-test.yaml summary-status
python -m pandeng.app --config configs/summary-test.yaml summary-trace --task-id 1
```

新鲜 `summary-test.db` 的预期汇总：

```text
固定告警 2 条；任务 2 条；模型摘要 1 条；模板回退 1 条；模型调用 2 次；有限收尾完成=是
```

这里的模型调用由离线模拟响应产生，没有访问本机 Ollama。任务 1 应保存模型摘要，R1 思考文本只在审计中出现；任务 2 应因输入外数字 `999` 回退固定模板。

`summary-trace` 应能看到事件检查、事件、指标快照和分析运行 ID，以及 `elapsed_ms`、`total_duration_ns`、`load_duration_ns`、`eval_count`、`eval_duration_ns`。模拟响应会提供测试数值；真实接口未返回字段时显示“未知”。

## 八、可选真实 Ollama 验收

只有完成上述离线验收并希望实测 `deepseek-r1:14b` 时才执行。真实演示使用独立的 `summary-live-test.db`，其中行情和持仓均为模拟记录。

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -Method Get
python -m pandeng.app --config configs/summary-live-test.yaml init
python -m pandeng.app --config configs/summary-live-test.yaml summary-demo --session-date 2026-01-08 --real-ollama
python -m pandeng.app --config configs/summary-live-test.yaml summary-status
python -m pandeng.app --config configs/summary-live-test.yaml summary-trace --task-id 1
```

若 `api/tags` 连接失败，可在另一个 PowerShell 窗口运行 `ollama serve`，保持该窗口打开，再重试。已有 Ollama 服务时无需重复启动。真实模型输出未通过严格校验时会保存模板，原始告警仍然有效。不要在宿舍断电或设备供电、散热条件不适合时强行测试大模型。

可选资源观察需要在另一个普通 PowerShell 窗口手动执行：

```powershell
Get-Process ollama -ErrorAction SilentlyContinue | Select-Object ProcessName, Id, CPU, WorkingSet64
nvidia-smi
```

没有 NVIDIA 工具或命令不可用时不影响盘灯验收，不需要安装驱动。

## 九、常见问题

### 提示演示库已有记录

这是防混合保护，不要删除数据库。需要重跑时先移动整个旧演示库到本次备份：

```powershell
$OldSummary = Join-Path $Project "runtime\summary-test.db"
if (Test-Path $OldSummary) {
    Move-Item -LiteralPath $OldSummary -Destination (Join-Path $Backup "summary-test-$Stamp.db")
}
```

确认没有其他盘灯程序使用该文件后再移动。随后重新执行第七节的 `init` 和 `summary-demo`。

### 真实模型超时或连接失败

离线测试通过即说明程序安全链路有效。检查 Ollama 是否运行、模型名称是否为 `deepseek-r1:14b`，再查看 `summary-status` 的回退原因。不要关闭杀毒软件，也不要手动移动 Ollama 模型目录。

### 复制后仍显示结构版本 4

先执行：

```powershell
Set-Location D:\pandeng
python -m pandeng.app show-config
python -m pandeng.app init
python -m pandeng.app status
```

核对显示的数据库路径是否为主库。若复制文件检查出现 `False`，回到第四节，不要运行来源不明的命令。

### PowerShell 出现大量红色文字

只复制代码块中的命令，不要复制 `(.venv) PS D:\pandeng>` 提示符或“预期输出”。如果窗口停在 `>>`，通常是引号或括号未闭合，可按 `Ctrl+C` 返回正常提示符后重新输入完整命令。

## 十、本次验收边界

第四阶段 A 已完成本地异步摘要安全链路和可选真实模型入口。真实行情接入、云端门控和手动复盘仍待后续核心阶段。桌面窗口、Logo、EXE、安全更新、一键卸载和发布流程继续列入远期功能。
