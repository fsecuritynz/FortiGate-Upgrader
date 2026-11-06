# Build FortiGate Upgrader for Windows: dist\FortiGate Upgrader\FortiGate Upgrader.exe and, if Inno Setup is
# installed (https://jrsoftware.org/isinfo.php, or: choco install innosetup), the installer
# dist\FortiGate-Upgrader-<version>-Windows-x64-Setup.exe used by GitHub Releases and the in-app updater.
# Requires Python 3.11+ and the Edge WebView2 runtime (bundled with Windows 11).
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
py -3 -m venv .build-venv
.\.build-venv\Scripts\Activate.ps1
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean packaging\fortiupgrade.spec
Write-Host 'Built dist\FortiGate Upgrader\FortiGate Upgrader.exe'
$init = Get-Content fortiupgrade\__init__.py -Raw
$ver = [regex]::Match($init, '__version__ = "([^"]+)"').Groups[1].Value
$build = [regex]::Match($init, '__build__ = (\d+)').Groups[1].Value
$iscc = @("$env:ProgramFiles(x86)\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", (Get-Command iscc -ErrorAction SilentlyContinue).Source) |
  Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($iscc) {
  & $iscc "/DAppVersion=$ver" "/DAppBuild=$build" packaging\installer.iss
  Write-Host "Built dist\FortiGate-Upgrader-$ver-Windows-x64-Setup.exe"
} else {
  Write-Host 'Inno Setup not found: skipped the installer (the app in dist\ still works).'
}
