# Panlume 盘灯 v1.0.0

Panlume 盘灯首个双语公开正式版。用于本地 A 股公开行情观察、个人研究记录与软件学习。

## 下载

- **Windows 安装版**：`Panlume-v1.0.0-setup-windows-x64.exe`
- **Windows 便携版**：`Panlume-v1.0.0-portable-windows-x64.zip`
- **整理源码**：`Panlume-v1.0.0-source.zip`
- **校验文件**：`SHA256SUMS.txt`

普通用户无需安装 Python 或打开终端。安装版双击安装后从开始菜单启动；便携版完整解压后进入 `Panlume` 文件夹，双击 `Panlume.exe`。

## 本版功能

- 本地模拟观察、CSV 回放、低频调度与断网恢复；
- 数据质量检查、确定性指标、规则事件与冷却；
- 手动确认持仓、固定止损和 T+1；
- 固定模板摘要、可选 Ollama 摘要、事实校验和异常回退；
- 本地复盘、数据体检、事件与摘要追溯；
- 独立数据库的一键离线演示；
- 简体中文与 English；
- Windows 安装版与便携版。

新安装默认 English。需要中文时，打开 **Language / 语言**，选择简体中文并保存，重启后生效。历史摘要、复盘、告警和原始记录保留原文。

## 已完成的验收

D7 v0.7.0 验收构建已通过 Windows EXE 启动、双语切换、离线演示、便携版、安装卸载、个人数据保留、正常杀毒扫描、真实 Ollama 成功摘要和异常回退。v1.0.0 的最终 Windows 构建与校验值以本 Release 上传文件为准。

## 使用边界

项目只用于公开信息处理、个人研究记录和软件学习，不连接外部账户，不执行外部操作。程序不自动下单，不承诺收益，不构成投资建议。

真实持仓只能由用户手动确认更新，模型无权修改。本地模型只负责摘要，不能修改原始数据和程序结论。敏感配置通过环境变量管理，并提供示例文件。

Ollama 为可选增强，没有模型时仍可使用固定模板和离线功能。真实行情短验收必须由用户当次明确确认，并使用独立测试数据库。云端默认关闭。

## 尚未包含

高 DPI 与滚动专项优化、模型选择器、图形化模型下载、摘要客制化、事实包导出、10 只观察对象、MACD、RSI、繁体中文和日语留待后续版本。

---

# Panlume v1.0.0

The first bilingual public release of Panlume, a local Windows learning project for observing public A-share market data and keeping traceable personal research records.

## Downloads

- **Windows installer**: `Panlume-v1.0.0-setup-windows-x64.exe`
- **Portable Windows package**: `Panlume-v1.0.0-portable-windows-x64.zip`
- **Curated source archive**: `Panlume-v1.0.0-source.zip`
- **Checksums**: `SHA256SUMS.txt`

End users do not need Python or a terminal. Use the installer, or extract the portable archive completely and run `Panlume.exe` inside the `Panlume` folder.

## Included

- Simulated local observation, CSV replay, low-frequency scheduling, and outage recovery;
- Data-quality checks, deterministic indicators, rule events, and cooldowns;
- Manually confirmed holdings, fixed stops, and T+1 handling;
- Fixed-template summaries, optional Ollama summaries, fact validation, and fallback;
- Local reviews, data health checks, and event/summary tracing;
- An integrated demo using an independent database;
- Simplified Chinese and English;
- Windows installer and portable editions.

New installations default to English. To use Simplified Chinese, open **Language / 语言**, save the selection, and restart. Existing summaries, reviews, alerts, and source records keep their original text.

## Acceptance

The D7 v0.7.0 acceptance build passed Windows startup, language switching, the integrated offline demo, portable-package startup, installation, uninstallation, personal-data retention, normal antivirus scanning, a successful real Ollama summary, and fixed-template fallback. The final v1.0.0 artifacts and checksums are those attached to this Release.

## Scope and safety

For public-information processing, personal research records, and software learning only. No external account connection or external operation. No automatic trading, return promises, or investment advice.

Real holdings require explicit user confirmation. The local model only writes summaries and cannot modify raw data or deterministic program conclusions. Sensitive configuration uses environment variables with an example file.

Ollama is optional. Fixed templates and offline features remain available without it. Controlled live-data acceptance requires explicit confirmation for that run and uses an independent test database. Cloud access remains disabled by default.

## Not included in v1.0.0

High-DPI and scrolling work, a model selector, graphical model downloads, customizable summaries, fact-package export, ten watchlist entries, MACD, RSI, Traditional Chinese, and Japanese are left for later releases.
