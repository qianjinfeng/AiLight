# uninstall.ps1 - remove SN902W daemon autostart entry
$ErrorActionPreference = "Stop"
$name = "SN902StatusLight"
$reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
try {
    Remove-ItemProperty -Path $reg -Name $name -ErrorAction SilentlyContinue
    Write-Host "startup entry removed: $name"
} catch {
    Write-Host "no startup entry found (nothing to remove)"
}
Write-Host "Note: hook configs you installed with 'setup' are NOT touched."
Write-Host "To remove them, edit the agent config files yourself (see README)."
