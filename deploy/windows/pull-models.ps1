# Downloads the models and puts the "Корсик модель" shortcut on the desktop.
# Runs after setup-ollama.ps1 and without administrator rights:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\korsik\pull-models.ps1
# Safe to run again: downloaded models are skipped, a broken download continues.
param(
    [string]$ChatModel = "qwen3:14b",
    [string]$EmbedModel = "bge-m3"
)

. "$PSScriptRoot\common.ps1"

# Without this Russian text turns into garbage when another program (Codex) reads the output.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
    Write-Host "Не получилось переключить вывод на UTF-8, русский текст может прийти кривым."
}

function Fail($text) {
    Write-Host $text -ForegroundColor Red
    exit 1
}

# Ollama started from an elevated shell would run as administrator,
# and the window, which runs as a normal user, could not stop it.
if (Test-Elevated) {
    Fail "Этот шаг запускается без прав администратора, в обычном PowerShell."
}

$ollama = Find-Ollama
if (-not $ollama) {
    Fail "Не нашёл ollama.exe. Сначала запусти setup-ollama.ps1."
}

# If the model is on in the window, download through it. Anything else is a leftover
# and gets replaced by a temporary server that listens on this computer only.
$startedHere = $false
if (-not ((Test-OllamaUp) -and (Test-WideListener))) {
    $left = @(Stop-Ollama)
    if ($left.Count -gt 0) {
        Fail "Не получилось остановить запущенную Ollama ($($left -join ', ')). Скорее всего, её запустили от имени администратора: закрой её в диспетчере задач и запусти скрипт снова."
    }
    Write-Host "Запускаю Ollama на время скачивания, только на 127.0.0.1."
    $server = Start-OllamaServer $ollama "127.0.0.1:$port"
    if (-not (Wait-Ollama $server 60)) {
        $null = Stop-Ollama
        Fail "Ollama не запустилась за минуту. Последние строки её журнала: $(Get-ServerLogTail)"
    }
    $startedHere = $true
}

try {
    $missing = @(Get-MissingModels @($ChatModel, $EmbedModel))
} catch {
    if ($startedHere) {
        $null = Stop-Ollama
    }
    Fail "Ollama не отдала список моделей: $($_.Exception.Message)"
}

# The ollama command connects to OLLAMA_HOST, and an old 0.0.0.0 value
# may still live in this terminal. Windows does not connect to 0.0.0.0.
$env:OLLAMA_HOST = "127.0.0.1:$port"
foreach ($model in @($ChatModel, $EmbedModel)) {
    if ($missing -notcontains $model) {
        Write-Host "Модель $model уже скачана."
        continue
    }
    Write-Host "Скачиваю $model, это может занять много минут..."
    & $ollama pull $model
    if ($LASTEXITCODE -ne 0) {
        if ($startedHere) {
            $null = Stop-Ollama
        }
        Fail "Не получилось скачать $model. Проверь интернет и запусти скрипт ещё раз, докачка продолжится."
    }
    Write-Host "Скачал $model."
}

if ($startedHere) {
    $left = @(Stop-Ollama)
    if ($left.Count -gt 0) {
        Fail "Не получилось остановить Ollama после скачивания ($($left -join ', ')). Закрой её в диспетчере задач."
    }
    Write-Host "Остановил Ollama, дальше модель включается кнопкой в окне."
}

$icon = Join-Path (Split-Path $ollama) "ollama app.exe"
if (-not (Test-Path $icon)) {
    $icon = $ollama
}
$linkPath = Join-Path ([Environment]::GetFolderPath("Desktop")) "Корсик модель.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
# wscript runs korsik-model.vbs, which starts the window without a console.
$shortcut.TargetPath = "$env:SystemRoot\System32\wscript.exe"
$shortcut.Arguments = "`"$PSScriptRoot\korsik-model.vbs`" -ChatModel $ChatModel -EmbedModel $EmbedModel"
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "Включить или выключить модель для бота Корсик"
$shortcut.Save()

Write-Host ""
Write-Host "Готово. Ярлык на рабочем столе: $linkPath, модель $ChatModel."
Write-Host "Если окно 'Корсик модель' сейчас открыто, закрой его и открой ярлыком заново."
Write-Host "Проверка: check-ollama.ps1 -ChatModel $ChatModel"
