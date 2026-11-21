[CmdletBinding()]
param(
    [string]$InnoSetupPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw "The Windows installer must be built on Windows."
}

$AppDir = Join-Path $ProjectRoot "dist\Panlume"
$Exe = Join-Path $AppDir "Panlume.exe"
if (-not (Test-Path $Exe)) {
    throw "Panlume.exe is missing. Run scripts\build-windows.ps1 first."
}
if (Test-Path (Join-Path $AppDir "panlume.portable")) {
    throw "The installer source contains a portable marker. Rebuild the base app."
}

$Candidates = @()
if ($InnoSetupPath) { $Candidates += $InnoSetupPath }
$Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if ($Command) { $Candidates += $Command.Source }
$Candidates += @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$Compiler = $Candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Compiler) {
    throw "Inno Setup 6 was not found. Install it for the current user or pass -InnoSetupPath."
}

Push-Location $ProjectRoot
try {
    & $Compiler (Join-Path $ProjectRoot "packaging\Panlume.iss")
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup compilation failed." }
    $Installer = Join-Path $ProjectRoot "dist\Panlume-v2.0.0-setup-windows-x64.exe"
    if (-not (Test-Path $Installer)) { throw "The installer was not created." }
    Write-Host "Installer: $Installer"
    Get-FileHash $Installer -Algorithm SHA256
} finally {
    Pop-Location
}
