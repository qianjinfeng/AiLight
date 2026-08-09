# install.ps1 - install deps and (optionally) register SN902W daemon on startup
$ErrorActionPreference = "Stop"

Write-Host "== SN902W status light: install ==" -ForegroundColor Cyan

# 1. dependencies
Write-Host "[1/3] installing Python dependencies (bleak) ..."
python -m pip install --upgrade pip | Out-Null
python -m pip install -r "$PSScriptRoot\requirements.txt"

# 2. verify
Write-Host "[2/3] verifying ..."
python -c "import bleak; print('  bleak', bleak.__version__, 'OK')"

# 3. optional autostart
$doAuto = $true
if ($args -contains "-noautostart") { $doAuto = $false }
if ($doAuto) {
    $exe = (Get-Command python).Source
    $run = "`"$exe`" `"$PSScriptRoot\webserver.py`" --no-browser"
    $reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
    $name = "SN902StatusLight"
    try {
        New-ItemProperty -Path $reg -Name $name -Value $run -PropertyType String -Force | Out-Null
        Write-Host "[3/3] startup entry added: $name"
    } catch {
        Write-Host "[3/3] could not write startup registry (skipped)"
    }
}

Write-Host ""
Write-Host "Start the daemon with:" -ForegroundColor Green
Write-Host "  python `"$PSScriptRoot\webserver.py`"" -ForegroundColor Green
Write-Host "Console: http://127.0.0.1:7800"
