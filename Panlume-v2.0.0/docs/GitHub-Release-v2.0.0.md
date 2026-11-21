# Panlume 盘灯 v2.0.0

盘灯是 Windows 本地 A 股公开数据观察与个人研究记录工具。本版建立统一事实包、完整固定模板和可选本地模型叙述，提供整页报告成果与可追溯证据。项目不连接交易账户、不执行订单、不承诺收益。

## 主要更新

- 统一事实包：稳定 JSON、内容 SHA-256、完整规则事件字段、数据质量和原始证据追溯。
- 报告中心与整页报告成果：预设、预览、JSON/Markdown 导出、程序事实与模型叙述分区。
- Ollama 图形管理：安装/连接检测、模型列表、参数与磁盘信息、真实下载进度、取消和确认删除。
- 最多 10 个观察对象，支持启停和排序；新增确定性 MACD/RSI。
- English、简体中文、繁體中文、日本語；历史记录保留原文。
- Windows Per Monitor V2、可见的现代滑块、滚动绑定去重和回调清理。
- 图形化 CSV 历史回放、离线演示、本地复盘、数据体检、备份和保守清理。

## 下载文件

- `Panlume-v2.0.0-setup-windows-x64.exe`：当前用户安装版。
- `Panlume-v2.0.0-portable-windows-x64.zip`：便携版，完整解压后进入 Panlume 文件夹启动。
- `Panlume-v2.0.0-source.zip`：完整源码与测试。
- `SHA256SUMS.txt`：四类文件的 SHA-256，位于发布包 dist 目录。

便携版数据保存在程序旁 `PanlumeData`。安装版默认使用 `%LOCALAPPDATA%\Panlume`。标准卸载默认保留个人数据。新安装默认 English，已有中文设置升级后继续使用中文。

## 验收

- Windows 全量离线测试：498 passed。
- 基础 EXE 与便携 EXE 启动、数据库结构 6、启动模型调用 0。
- 独立 AppId 安装、启动、标准卸载和个人数据保留通过。
- 真实 `deepseek-r1:14b` 报告摘要通过严格事实校验并采用；超时和无效输出回退保留完整固定事实。
- 最终构建目录已完成 Windows Defender 自定义扫描。
- 用户已完成当前界面检查；多 DPI 项目按人工清单验收。

## 文件 SHA-256

```text
Panlume.exe
3eded91d13715d4a4fee53d2440274874784daab193d7a69fd7c6198b498f475

Panlume-v2.0.0-portable-windows-x64.zip
2ba1379bf3940418cfb7a0476fa9c4ca2e3ef86ce02ac960f29cf8a139f8ae5f

Panlume-v2.0.0-setup-windows-x64.exe
6b63af2ae920170da278da93a5994e0a12cf84444d8f5fcad7edeb9b1d250c74
```

源码 ZIP 的 SHA-256 见正式发布目录 `SHA256SUMS.txt`。

## 安全边界

项目只用于公开信息处理、个人研究记录和软件学习。真实持仓只能由用户手动确认更新。模型无权修改行情、指标、规则、持仓或数据库。真实行情请求继续要求当次明确确认和独立测试数据库。云端接口保持默认关闭。

不需要管理员权限，不安装驱动，不设置开机启动，不捆绑模型，不需要关闭杀毒软件。不要发布个人 YAML、SQLite、真实持仓、日志、备份、虚拟环境、密钥或 Ollama 模型文件。
