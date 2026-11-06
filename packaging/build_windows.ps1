# Build FortiUpgrade.exe on Windows. Requires Python 3.11+ and the Edge WebView2 runtime (bundled with Windows 11).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
py -3 -m venv .build-venv
.\.build-venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean packaging\fortiupgrade.spec
Write-Host 'Built dist\FortiGate Upgrader\FortiGate Upgrader.exe'
