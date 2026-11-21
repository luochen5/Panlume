from __future__ import annotations

from pathlib import Path

from pandeng.desktop import APP_VERSION


def test_windows_build_scripts_share_version_and_live_data_gate() -> None:
    root = Path(__file__).resolve().parents[1]
    build = (root / "scripts" / "build-windows.ps1").read_text(encoding="utf-8")
    installer = (root / "scripts" / "build-installer.ps1").read_text(encoding="utf-8-sig")
    setup = (root / "packaging" / "Panlume.iss").read_text(encoding="utf-8-sig")
    spec_path = root / "packaging" / "Panlume.spec"
    spec = spec_path.read_text(encoding="utf-8")

    assert f'$Version = "{APP_VERSION}"' in build
    assert f"dist\\Panlume-v{APP_VERSION}-setup-windows-x64.exe" in installer
    assert f'#define AppVersion "{APP_VERSION}"' in setup
    assert 'if ($IncludeLiveData) { "requirements-live-build.txt" }' in build
    assert 'Remove-Item (Join-Path $AppDir "live-data-enabled.txt")' in build
    assert 'os.environ.get("PANDENG_INCLUDE_LIVE_DATA") == "1"' in spec
    compile(spec, str(spec_path), "exec")
