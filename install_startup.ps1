$WshShell = New-Object -ComObject WScript.Shell
$StartupFolder = [System.Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder "SpotifyTGBio.lnk"

$TargetVBS = Join-Path $PSScriptRoot "run_background.vbs"

$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = "`"$TargetVBS`""
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.Description = "Auto Spotify TG Bio Updater"
$Shortcut.Save()

Write-Host "✅ Ярлык успешно добавлен в автозагрузку Windows!" -ForegroundColor Green
Write-Host "Путь: $ShortcutPath" -ForegroundColor Gray
