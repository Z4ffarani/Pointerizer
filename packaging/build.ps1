# One-command release build: exe + installer land in dist\ (run from anywhere).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)  # repo root, so all paths below are relative to it

python pointerizer.py --selfcheck
if ($LASTEXITCODE) { throw "selfcheck failed" }

python packaging\make_icon.py

pyinstaller --noconfirm --onefile --windowed --name Pointerizer `
    --icon assets\icon.ico `
    --add-data "assets\icon.ico;assets" `
    --add-data "assets\chevron_down.png;assets" `
    --add-data "assets\chevron_up.png;assets" `
    --add-data "assets\Ubuntu;assets\Ubuntu" `
    pointerizer.py
if ($LASTEXITCODE) { throw "pyinstaller failed" }

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") |
        Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $iscc) { throw "Inno Setup not found - winget install JRSoftware.InnoSetup" }

& $iscc packaging\pointerizer.iss
if ($LASTEXITCODE) { throw "installer build failed" }

Get-Item dist\Pointerizer.exe, dist\PointerizerSetup.exe |
    Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}}
