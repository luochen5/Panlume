[CmdletBinding()]
param(
    [string]$PythonPath = "",
    [switch]$SkipTests,
    [switch]$IncludeLiveData
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "Windows build must run on Windows. Do not substitute a Linux binary."
}

$BaseArgs = @()
if ($PythonPath) {
    $BasePython = $PythonPath
} elseif (Test-Path (Join-Path $ProjectRoot ".venv\Scripts\python.exe")) {
    $BasePython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $BasePython = (Get-Command py).Source
    $BaseArgs = @("-3.14")
} else {
    $BasePython = (Get-Command python -ErrorAction Stop).Source
}

& $BasePython @BaseArgs -c "import sys, tkinter; assert sys.version_info >= (3, 12), sys.version; assert sys.maxsize > 2**32, '64-bit Python is required'; print(sys.version); print('Tk', tkinter.TkVersion)"
if ($LASTEXITCODE -ne 0) { throw "64-bit Python 3.12+ with Tkinter is required." }

$BuildVenv = Join-Path $ProjectRoot ".venv-build"
$BuildPython = Join-Path $BuildVenv "Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    & $BasePython @BaseArgs -m venv $BuildVenv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the isolated build environment." }
}

$RequirementsFile = if ($IncludeLiveData) { "requirements-live-build.txt" } else { "requirements-build.txt" }
& $BuildPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectRoot $RequirementsFile)
if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
if ($IncludeLiveData) {
    & $BuildPython -c "import akshare; assert akshare.__version__ == '1.20.65', akshare.__version__"
    if ($LASTEXITCODE -ne 0) { throw "The verified AKShare version is required for a live-data build." }
    $env:PANDENG_INCLUDE_LIVE_DATA = "1"
} else {
    Remove-Item Env:PANDENG_INCLUDE_LIVE_DATA -ErrorAction SilentlyContinue
}

Push-Location $ProjectRoot
try {
    if (-not $SkipTests) {
        & $BuildPython -m pytest -q -p no:cacheprovider
        if ($LASTEXITCODE -ne 0) { throw "Tests failed. No package was produced." }
    }

    & $BuildPython -m PyInstaller --noconfirm --clean --distpath (Join-Path $ProjectRoot "dist") --workpath (Join-Path $ProjectRoot "build") (Join-Path $ProjectRoot "packaging\Panlume.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $AppDir = Join-Path $ProjectRoot "dist\Panlume"
    $Exe = Join-Path $AppDir "Panlume.exe"
    if (-not (Test-Path $Exe)) { throw "Panlume.exe is missing." }
    foreach ($Name in @("schema.sql", "analysis-test.yaml", "pandeng.ico", "logo-128.png")) {
        if (-not (Get-ChildItem $AppDir -Recurse -File -Filter $Name | Select-Object -First 1)) {
            throw "Bundled resource is missing: $Name"
        }
    }
    if (Test-Path (Join-Path $AppDir "panlume.portable")) {
        throw "The base build must not contain a portable marker."
    }
    if ($IncludeLiveData) {
        [System.IO.File]::WriteAllText(
            (Join-Path $AppDir "live-data-enabled.txt"),
            "AKShare 1.20.65; explicit single-request acceptance only`n",
            [System.Text.Encoding]::ASCII
        )
    } else {
        Remove-Item (Join-Path $AppDir "live-data-enabled.txt") -Force -ErrorAction SilentlyContinue
    }

    $PortableRoot = Join-Path $ProjectRoot "dist\Panlume-portable"
    if (Test-Path $PortableRoot) { Remove-Item $PortableRoot -Recurse -Force }
    $PortableApp = Join-Path $PortableRoot "Panlume"
    New-Item -ItemType Directory -Path $PortableApp -Force | Out-Null
    Copy-Item (Join-Path $AppDir "*") $PortableApp -Recurse -Force
    [System.IO.File]::WriteAllText((Join-Path $PortableApp "panlume.portable"), "portable`n", [System.Text.Encoding]::ASCII)
    Copy-Item (Join-Path $ProjectRoot "packaging\portable-readme.txt") (Join-Path $PortableRoot "README-portable.txt")

    $Version = "2.0.0"
    $Variant = if ($IncludeLiveData) { "-live" } else { "" }
    $PortableZip = Join-Path $ProjectRoot "dist\Panlume-v$Version$Variant-portable-windows-x64.zip"
    Compress-Archive -Path (Join-Path $PortableRoot "*") -DestinationPath $PortableZip -Force

    Write-Host "Build complete: $Exe"
    Write-Host "Portable ZIP: $PortableZip"
    Get-FileHash $Exe -Algorithm SHA256
    Get-FileHash $PortableZip -Algorithm SHA256
    Write-Host "Next: run scripts\build-installer.ps1 after installing Inno Setup 6."
} finally {
    Remove-Item Env:PANDENG_INCLUDE_LIVE_DATA -ErrorAction SilentlyContinue
    Pop-Location
}
