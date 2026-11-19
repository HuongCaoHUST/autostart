
# ==============================================
# Chrome + Python Auto Startup
# Portable Windows Script
# ==============================================

# ================ CONFIGURATION ================

$Url1 = "https://youtube.com"
$Url2 = "https://chatgpt.com"

$PythonFile1 = "agent1.py"
$PythonFile2 = "agent2.py"

$ShortcutName = "Chrome_Python_AutoStart.lnk"

# ===============================================

# Get script directory
$BaseDir = $PSScriptRoot

# 1. Find Chrome
$ChromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
    "$env:ProgramW6432\Google\Chrome\Application\chrome.exe"
)

$ChromePath = $ChromePaths |
    Where-Object { $_ -and (Test-Path $_) } |
    Select-Object -First 1

if (-not $ChromePath) {
    Write-Host "[ERROR] Chrome not found!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "[OK] Chrome: $ChromePath" -ForegroundColor Green

# 2. Find Python
$PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue

if (-not $PythonCommand) {
    $PythonCommand = Get-Command py.exe -ErrorAction SilentlyContinue
}

if (-not $PythonCommand) {
    Write-Host "[ERROR] Python not found!" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

$PythonPath = $PythonCommand.Source

Write-Host "[OK] Python: $PythonPath" -ForegroundColor Green

# 3. Define Startup folder
$StartupFolder = [Environment]::GetFolderPath("Startup")

# 4. Create launcher script
$LauncherPath = Join-Path $BaseDir "startup_launcher.ps1"

$LauncherContent = @"
`$BaseDir = '$($BaseDir.Replace("'", "''"))'
`$ChromePath = '$($ChromePath.Replace("'", "''"))'
`$PythonPath = '$($PythonPath.Replace("'", "''"))'

`$Url1 = '$($Url1.Replace("'", "''"))'
`$Url2 = '$($Url2.Replace("'", "''"))'

`$PythonFile1 = '$($PythonFile1.Replace("'", "''"))'
`$PythonFile2 = '$($PythonFile2.Replace("'", "''"))'

# Launch Chrome with both URLs
Start-Process -FilePath `$ChromePath -ArgumentList @(`$Url1, `$Url2)

# Launch Python programs
`$File1 = Join-Path `$BaseDir `$PythonFile1
`$File2 = Join-Path `$BaseDir `$PythonFile2

if (Test-Path `$File1) {
    Start-Process -FilePath `$PythonPath -ArgumentList @("`$File1") -WorkingDirectory `$BaseDir
}

if (Test-Path `$File2) {
    Start-Process -FilePath `$PythonPath -ArgumentList @("`$File2") -WorkingDirectory `$BaseDir
}
"@

Set-Content -Path $LauncherPath -Value $LauncherContent -Encoding UTF8

# 5. Create Windows Startup shortcut
$ShortcutPath = Join-Path $StartupFolder $ShortcutName

$WScriptShell = New-Object -ComObject WScript.Shell
$Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)

$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$LauncherPath`""
$Shortcut.WorkingDirectory = $BaseDir
$Shortcut.Description = "Chrome and Python Auto Startup"

$Shortcut.Save()

Write-Host "[OK] Startup configured!" -ForegroundColor Green

# 6. Run immediately for testing
Start-Process -FilePath "powershell.exe" -ArgumentList @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$LauncherPath`""
)

Write-Host "`n======================================" -ForegroundColor Cyan
Write-Host "Configuration completed!"
Write-Host "Chrome URL 1 : $Url1"
Write-Host "Chrome URL 2 : $Url2"
Write-Host "Python 1    : $PythonFile1"
Write-Host "Python 2    : $PythonFile2"
Write-Host "Startup     : Enabled"
Write-Host "======================================"

Read-Host "Press Enter to exit"
