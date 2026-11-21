# 盘灯第二阶段 A：离线模拟与 CSV 回放

## 本次范围

本次完成一个可独立验证的小闭环：

```text
模拟器或 CSV → 数据格式与质量检查 → SQLite → 状态查询
```

支持原始未复权分钟行情、前复权日线和交易日历。不会访问真实行情，不启动后台轮询，不调用 Ollama 或云端模型。暂停恢复、断网重连和自动收盘调度属于第二阶段后续工作，本包尚未实现。

## 本机环境与文件位置

你当前已经验证的环境：

- Python 3.14.10；
- 项目目录：`D:\pandeng`；
- 虚拟环境：`D:\pandeng\.venv`；
- 数据库：`D:\pandeng\runtime\pandeng.db`。

## 1. 先备份第一阶段数据库

普通 PowerShell 中执行：

```powershell
Set-Location D:\pandeng
Copy-Item .\runtime\pandeng.db .\runtime\pandeng-stage1-backup.db
```

保留压缩包作为源码备份也可以。不要在程序运行时删除数据库。

## 2. 更新项目文件

完整更新包内含有 `pandeng` 文件夹。请先解压到一个临时目录，再复制该 `pandeng` 文件夹内的全部内容到 `D:\pandeng`，选择覆盖同名文件。

更新后检查这些文件存在：

```powershell
Test-Path .\pandeng\data\base.py
Test-Path .\pandeng\data\simulator.py
Test-Path .\pandeng\data\replay.py
Test-Path .\pandeng\data\cleaner.py
Test-Path .\pandeng\storage\repositories.py
Test-Path .\examples\minute_bars.csv
```

六条都应该返回 `True`。配置文件 `configs/settings.yaml` 内容没有变化，但更新包也附有一份标准配置；如果你已修改过自选股，可先备份再覆盖。

## 3. 启用已有环境并升级数据库

```powershell
Set-Location D:\pandeng
Set-ExecutionPolicy -Scope Process Bypass
.\.venv\Scripts\Activate.ps1
python -m pandeng.app init
```

本次只需新增数据表，不增加第三方依赖。预期显示“数据库初始化/升级完成”，原数据库不会清空。

## 4. 运行离线模拟

```powershell
python -m pandeng.app simulate --session-date 2026-01-05 --minutes 5 --daily-days 25
```

参数含义：

- `session-date`：模拟交易日；
- `minutes`：每只股票从 09:31 开始生成的分钟数，范围 1～240；
- `daily-days`：每只股票生成的前复权日线数，范围 1～60。

按默认 5 只自选股计算，预期：

```text
原始分钟行情：收到 25；新增 25；去重 0；拒绝 0；正常
前复权日 K：收到 125；新增 125；去重 0；拒绝 0；正常
模拟交易日历：收到 35；新增 35；去重 0；拒绝 0；正常
```

查看保存结果：

```powershell
python -m pandeng.app data-status
```

再执行一次相同的 `simulate` 命令，应该显示新增 0、去重 25/125/35。数据库中的业务数据数量不会翻倍；处理批次与去重日志会增加，这是预期行为。

## 5. 验证 CSV 回放与异常状态

示例文件位于：

- `examples/minute_bars.csv`：5 条分钟记录，其中 600000 缺少 09:32，用于测试缺口；
- `examples/daily_qfq.csv`：两只股票的 3 根前复权日 K；
- `examples/trading_calendar.csv`：模拟交易日历，不能当作真实节假日日历。

运行：

```powershell
python -m pandeng.app replay --minute-file .\examples\minute_bars.csv --daily-file .\examples\daily_qfq.csv --calendar-file .\examples\trading_calendar.csv --replay-clock "2026-01-05T09:33:00+08:00"
```

预期：

- 分钟行情收到 5 条、新增 5 条，代码 600000 显示状态未知；
- 状态未知来自分钟缺口，不会被解释成“无触发”；
- 前复权日线收到并新增 6 条；
- 交易日历收到并新增 5 条；
- 全过程不访问网络或模型。

如果此前已经执行过相同回放，新增数量会减少、去重数量会增加，属于正常现象。缺口异常仍会保留处理记录。

CSV 规则：

- 文件使用 UTF-8 或带 BOM 的 UTF-8；
- 分钟时间必须带时区，如 `2026-01-05T09:31:00+08:00`；
- 分钟 `adjustment` 必须为 `none`；
- 日线 `adjustment` 必须为 `qfq`；
- `confirmed` 可填 `true` 或 `false`，未确认的日 K 会保存但不能生成正式信号；
- 回放只读取配置中的自选股代码。

## 6. 自动化测试

```powershell
python -m pytest -q
```

预期 **29 passed**。测试在临时目录建立数据库，不访问你的正式数据库或模型。

测试覆盖：

- 原 12 项配置与基础命令回归；
- 交易时段和午休边界；
- 模拟行情可重复；
- 无时区、未来数据、OHLC 矛盾、缺失字段；
- 分钟复权口径与日线前复权口径；
- 完全重复去重；
- 内容冲突拒绝覆盖；
- 分钟缺口标记状态未知；
- 同一代码部分有效、部分异常时仍保持未知；
- 未确认日 K 保存但不标记为已确认；
- 模拟命令和 CSV 回放命令。

## 7. 本次没有实现的功能

- 实时行情供应商；
- 后台 5 分钟轮询；
- 暂停、继续与断网补数；
- 收盘自动退出；
- 指标、规则、止损、T+1 和 MA20；
- Ollama 与云端分析。

这些将在对应阶段接入，不需要现在测试。

## 8. 常见问题

### `simulate` 提示数据库尚未初始化

先执行：

```powershell
python -m pandeng.app init
```

### 回放提示找不到 CSV

检查当前目录与文件路径：

```powershell
Set-Location D:\pandeng
Test-Path .\examples\minute_bars.csv
```

### 显示状态未知

先用 `data-status` 查看最近批次的拒绝和接收数量。示例分钟文件有意包含缺口，出现未知是该测试的预期结果。数据库表 `data_quality_issues` 保存了具体原因和原始输入。

### 如何回到第一阶段

停止盘灯命令后，使用备份的项目源码恢复文件；数据库可恢复为 `runtime/pandeng-stage1-backup.db`。不要同时保留多个程序操作同一个数据库。
