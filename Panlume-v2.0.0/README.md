# Panlume 盘灯

[简体中文说明](README.zh-CN.md) · [报告与模型使用说明](docs/v2.0.0报告与模型使用说明.md) · [English report guide](docs/v2.0.0-report-and-model-guide.md) · [发布记录](docs/v2.0.0阶段8迁移与发布验收.md)

Panlume (`pandeng`) is a local Windows learning project for observing public A-share market data and keeping traceable personal research records. It combines deterministic Python calculations, SQLite evidence, fixed-template summaries, optional local Ollama summaries, and a Tkinter desktop interface.

**Current version: v2.0.0, the unified reporting release.** It is intended for public-information processing, personal research, and software learning. It is not presented as a mature investment system.

## Highlights

- Local observation with simulated data, CSV replay, low-frequency scheduling, outage recovery, and data-quality checks.
- Deterministic intraday indicators, rule events, manually confirmed position lots, fixed-stop records, and T+1 handling.
- Fixed-template summaries with strict fact validation and fallback when a local model is unavailable or its output fails validation.
- Local daily reviews, read-only data health checks, event/summary tracing, backups, and conservative cleanup.
- An integrated offline demo that uses an independent `analysis-test.db`; the personal database is excluded.
- Versioned fact packages, complete fixed templates, validated optional model narratives, and JSON/Markdown export.
- A dedicated Report Center and a full-page Report Results view with separate program-fact and model-narrative areas.
- Graphical Ollama installation/connection checks, model selection, real download progress, cancellation, and confirmed deletion.
- Up to ten ordered and individually enabled watchlist entries; deterministic MACD and RSI with explicit data-quality boundaries.
- Simplified Chinese, Traditional Chinese, English, and Japanese; saved records keep their original language.
- Windows Per Monitor V2 initialization, visible themed scrollbars, and callback cleanup.
- No automatic trading, account connection, return promise, screen recognition, simulated clicks, or unauthorized securities-client access.

## Language behavior

The dedicated **Language / 语言** page controls the display language.

- New public installations default to **English**.
- Existing settings files without a `language` key remain in **Simplified Chinese** after upgrading.
- Choose a language, save it, and restart Panlume. The current window and newly generated templates in that same process keep the startup language until restart.
- New fixed templates use the active startup language.
- Existing summaries, reviews, alerts, audit records, JSON, and other source records keep their original text.
- Interface labels may be translated around those original records; the saved content is not rewritten.
- Missing English translations fall back to Chinese.
- Translation resources are bundled and work offline. No language package download is required.

Version 2.0.0 adds four-language resources, model management, report presets, MACD, RSI, a ten-entry watchlist, and high-DPI/scrolling improvements. See the report and model guides for field contracts and limitations.

## Run from source on Windows

Python 3.12 or later is recommended. The previous Windows environment used Python 3.14.10, 64-bit. Ordinary user permissions are sufficient; virtual-environment activation and administrator rights are not required.

Extract to a new directory instead of overwriting an older source tree, personal YAML files, databases, or backups.

```powershell
Set-Location D:\pandeng-v2
py -3.14 -m venv .venv-desktop
& .\.venv-desktop\Scripts\python.exe -m pip install -r requirements.txt
$env:PANDENG_DATA_DIR = "D:\pandeng-v2-acceptance-data"
& .\.venv-desktop\Scripts\python.exe desktop_launcher.py
```

`PANDENG_DATA_DIR` isolates acceptance data for the current PowerShell session. Startup initializes only the local personal database; it does not request live market data, call Ollama, or access a cloud service.

## Try the integrated offline demo

1. Open **Offline demo**.
2. Select **Start / view offline demo** and wait for the background task to finish.
3. Review observation and recovery, fixed-template summaries, local reviews, data health, and event/summary evidence.
4. Run it again. A completed demo is read back without appending or overwriting records.

The fixed demo represents simulated data for `2026-01-07`. A complete first run contains:

- 8 scheduler rounds, 14 analysis runs, and 14 indicator snapshots;
- 10 actual live-mode rule notifications from the simulated core;
- 10 summary tasks and 10 fixed-template results, with 0 model calls;
- 2 local reviews, for `000001` and `600000`;
- `240/240` expected minutes for each simulated stock in the data-health report;
- 6 rule events and 10 summary tasks in the history view.

The demo writes only to:

```text
runtime\analysis-demo\analysis-test.db
```

An old or incomplete demo is left unchanged. Use a new `PANDENG_DATA_DIR` for a fresh run; do not delete or overwrite an existing database to bypass this protection.

## Offline tests

```powershell
Set-Location D:\pandeng-v2
& .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-dev.txt
& .\.venv-desktop\Scripts\python.exe -m pytest -q -p no:cacheprovider
```

Current source result: **498 passed** in the Windows offline development environment. Tests use temporary directories, simulated clocks and market data, saved fixtures, simulated model responses, and local temporary HTTP services. They do not request real market data, real Ollama, or a real cloud service.

This result does not replace Windows executable, installer, portable-package, local-model, or antivirus acceptance.

## Build Windows packages

Build Windows executables on Windows:

```powershell
Set-Location D:\pandeng-v2
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build-windows.ps1"
```

Outputs:

```text
dist\Panlume\Panlume.exe
dist\Panlume-v2.0.0-portable-windows-x64.zip
```

The default build is offline and does not include the optional live-data dependency. Use `-IncludeLiveData` only when separately preparing a controlled live acceptance build.

With Inno Setup 6 installed:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\build-installer.ps1" -InnoSetupPath "D:\Inno Setup 6\ISCC.exe"
```

Output:

```text
dist\Panlume-v2.0.0-setup-windows-x64.exe
```

The installer uses minimal current-user HKCU entries required for installation and uninstallation. It does not create system-level or startup entries. Standard uninstall keeps personal data by default. A portable package remains available.

## Data and configuration

- Installed edition: `%LOCALAPPDATA%\Panlume`
- Portable edition: `PanlumeData` beside the portable application
- Isolated source acceptance: the directory in `PANDENG_DATA_DIR`
- Desktop preferences: strict JSON with atomic saving; no secret keys
- Database schema version: 6, with UTC+8 market timestamps
- Sensitive configuration: environment variables, with `.env.example` as a template

Do not publish personal YAML files, SQLite databases, real holdings, logs, backups, virtual environments, acceptance directories, credentials, or Ollama model files.

## Live-request and model boundaries

Real requests are disabled during normal startup and offline demos. A controlled live acceptance requires the user's explicit confirmation for that run, a defined interface, stock codes, time range, frequency, and expected request count. It uses an independent test database. Writing to a main database requires separate explicit confirmation.

Network failure, authentication failure, insufficient permission, invalid parameters, timeout, and genuine empty data must remain distinguishable. There are no automatic retries, CAPTCHA bypasses, access-control bypasses, or high-frequency attempts to evade upstream limits.

The local model only organizes supplied facts. It cannot modify raw market data, indicators, deterministic rule conclusions, or manually confirmed holdings. Model failure or validation failure returns the complete fixed template. The default local model remains `deepseek-r1:14b`; users can select other installed models without automatic downloads.

`cloud-review` remains a separately gated command for possible future use. Default acceptance does not purchase a service, enter real credentials, or make a real cloud request.

## Safety and scope

- Public information processing, personal research records, and software learning only; no external account connection or external operation.
- Real holdings can only be updated through explicit user confirmation; the model has no permission to change them.
- No order execution, investment-return promise, or investment advice.
- No administrator rights, drivers, registry startup entries, antivirus disabling, or broad antivirus exclusion directories are required.
- Ollama `blobs`, `manifests`, existing databases, personal YAML, virtual environments, and backups must not be silently deleted, moved, reset, or overwritten.
- Any future Galaxy Securities integration would require an officially authorized read-only market-data API. Account assets, trading information, login, order queries, and order placement remain out of scope.

## Release status

The D7 v0.7.0 acceptance build passed Windows executable startup, bilingual switching, the integrated offline demo, portable-package startup, installation, standard uninstall, personal-data retention, and normal antivirus scanning.

A real local Ollama summary was also generated successfully inside Panlume using `deepseek-r1:14b`. A subsequent forced timeout correctly returned the complete fixed template while preserving the original facts. See `docs/v1.0.0验收与发布记录.md` for the acceptance scope and hashes of that pre-release build.

Version 2.0.0 adds unified fact packages, complete fixed reports, validated optional model narratives, report presets and export, model management, ten watchlist entries, MACD/RSI, four languages, and desktop display improvements. Windows packages are rebuilt, smoke-tested, scanned, and hashed for this release.

## Project layout

```text
pandeng/              Core market, rules, storage, summary, and review code
pandeng/desktop/      Tkinter desktop, settings, translation, and demo services
configs/              Example and isolated acceptance configurations
docs/                 Chinese stage guides and D7 usage notes
examples/             Offline samples and fixtures
packaging/            PyInstaller and Inno Setup definitions
scripts/              Windows build scripts
tests/                Offline automated tests
verifier/             Repeatable acceptance standards and local run records
```

## License

MIT. See [LICENSE](LICENSE).
