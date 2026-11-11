$StartupFolder = [System.Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $StartupFolder "SpotifyTGBio.lnk"

if (Test-Path $ShortcutPath) {
    Remove-Item $ShortcutPath -Force
    Write-Host "✅ Ярлык успешно удалён из автозагрузки!" -ForegroundColor Yellow
} else {
    Write-Host "Ярлык не найден в автозагрузке." -ForegroundColor Gray
}
