# Checks the model and prints the lines for the bot's .env. No administrator rights needed:
#   powershell -NoProfile -ExecutionPolicy Bypass -File C:\korsik\check-ollama.ps1
# If the model is off, it is turned on for the check and turned off again at the end.
# If setup was run with other models, pass the same names: -ChatModel qwen3:8b
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
$ProgressPreference = "SilentlyContinue"
$problems = @()

foreach ($name in @("OLLAMA_HOST", "OLLAMA_KEEP_ALIVE")) {
    foreach ($scope in @("Machine", "User")) {
        if ([Environment]::GetEnvironmentVariable($name, $scope)) {
            $problems += "В Windows задана переменная $name ($scope), с ней Ollama, открытая не из окна, может слушать сеть. Запусти setup-ollama.ps1, он её уберёт."
        }
    }
}
$startupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "Ollama.lnk"
if (Test-Path $startupLink) {
    $problems += "Ollama снова в автозагрузке ($startupLink). Запусти setup-ollama.ps1, он её уберёт."
}

$firewall = Get-FirewallProblem -WithRules
if ($firewall) {
    Write-Host $firewall -ForegroundColor Red
    exit 1
}
Write-Host "Брандмауэр включён, правила для порта $port на месте."

$startedHere = $false
if (-not (Test-OllamaUp)) {
    $ollama = Find-Ollama
    if (-not $ollama) {
        Write-Host "Не нашёл ollama.exe. Запусти setup-ollama.ps1." -ForegroundColor Red
        exit 1
    }
    Write-Host "Модель выключена, включаю её на время проверки."
    $server = Start-OllamaServer $ollama "0.0.0.0:$port"
    if (-not (Wait-Ollama $server 60)) {
        $null = Stop-Ollama
        Write-Host "Ollama не запустилась за минуту. Последние строки её журнала: $(Get-ServerLogTail)" -ForegroundColor Red
        exit 1
    }
    $startedHere = $true
}

$version = (Invoke-RestMethod -Uri "$localUrl/api/version" -TimeoutSec 5).version
Write-Host "Версия Ollama: $version"

try {
    $tags = Invoke-RestMethod -Uri "$localUrl/api/tags" -TimeoutSec 10
    Write-Host "Модели на компьютере:"
    foreach ($model in $tags.models) {
        Write-Host ("  {0}, {1} ГБ" -f $model.name, [math]::Round($model.size / 1GB, 1))
    }
    $missing = @(Get-MissingModels @($ChatModel, $EmbedModel))
    foreach ($model in $missing) {
        $problems += "Нет модели $model. Запусти pull-models.ps1 -ChatModel $ChatModel, он её скачает."
    }
} catch {
    $problems += "Ollama не отдала список моделей: $($_.Exception.Message)"
    # Without the list the model tests below would only repeat the same error.
    $missing = @($ChatModel, $EmbedModel)
}

if (Test-WideListener) {
    Write-Host "Ollama слушает порт $port на всех адресах, из сети пускает только Tailscale (правила брандмауэра)."
} else {
    $problems += "Ollama слушает только 127.0.0.1, бот её не увидит. Её открыли не из окна 'Корсик модель': закрой Ollama в трее и включи модель кнопкой в окне."
}

$ip = Get-TailscaleIp (Find-Tailscale)
if ($ip) {
    Write-Host "Адрес в Tailscale: $ip"
    try {
        Invoke-RestMethod -Uri "http://${ip}:$port/api/version" -TimeoutSec 5 | Out-Null
        # A request to our own address never leaves the computer, the firewall does not see it.
        Write-Host "Ollama слушает адрес Tailscale. Пускает ли брандмауэр бота и закрыт ли порт для остальных, отсюда не проверить: это проверяется с Mac (README, раздел про Mac)."
    } catch {
        $problems += "Ollama не отвечает на адресе Tailscale ${ip}: $($_.Exception.Message)"
    }
} else {
    $problems += "У компьютера нет адреса Tailscale. Человек должен войти: значок Tailscale возле часов, Log in, аккаунт команды."
}

if ($missing -notcontains $ChatModel) {
    try {
        Write-Host "Загружаю $ChatModel в память..."
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $null = Invoke-OllamaPost "/api/generate" @{ model = $ChatModel } 600
        Write-Host ("Загрузка заняла {0:N1} с" -f $timer.Elapsed.TotalSeconds)

        $question = "Назови столицу России одним словом."
        Write-Host "Пробный вопрос: $question"
        $body = @{
            model = $ChatModel
            messages = @(@{ role = "user"; content = $question })
            stream = $false
            think = $false
            options = @{ temperature = 0 }
        }
        $timer = [System.Diagnostics.Stopwatch]::StartNew()
        $result = Invoke-OllamaPost "/api/chat" $body 300
        $timer.Stop()
        $reply = (([string]$result.message.content) -replace '(?s)<think>.*?</think>', '').Trim()
        Write-Host "Ответ модели: $reply"
        Write-Host ("Время ответа: {0:N1} с" -f $timer.Elapsed.TotalSeconds)
        if (-not $reply) {
            $problems += "Модель ответила пустой строкой."
        }
        if ($timer.Elapsed.TotalSeconds -gt 30) {
            Write-Host "Долго: похоже, модель считает на процессоре. Посмотри строки про видеокарту ниже." -ForegroundColor Yellow
        }
    } catch {
        $problems += "Модель $ChatModel не ответила на вопрос: $($_.Exception.Message)"
    }
}

if ($missing -notcontains $EmbedModel) {
    try {
        $result = Invoke-OllamaPost "/api/embed" @{ model = $EmbedModel; input = "Во сколько начинается рабочий день?" } 120
        $length = @($result.embeddings[0]).Count
        Write-Host "Пробный вектор: длина $length"
        if ($length -lt 2) {
            $problems += "Модель $EmbedModel вернула пустой вектор."
        }
    } catch {
        $problems += "Модель $EmbedModel не вернула вектор: $($_.Exception.Message)"
    }
}

Write-Host "Модели в памяти:"
foreach ($model in (Invoke-RestMethod -Uri "$localUrl/api/ps" -TimeoutSec 5).models) {
    Write-Host ("  " + (Format-LoadedModel $model))
    if ($model.size_vram -lt $model.size) {
        Write-Host "  $($model.name) не поместилась в видеокарту целиком, ответы будут медленнее. Если так и останется, возьми qwen3:8b (README, «Какую модель брать»)." -ForegroundColor Yellow
    }
}

if ($startedHere) {
    $left = @(Stop-Ollama)
    if ($left.Count -gt 0) {
        $problems += "Не получилось выключить Ollama после проверки ($($left -join ', '))."
    } else {
        Write-Host "Выключил модель обратно, как было до проверки."
    }
}

Write-Host ""
if ($problems.Count -gt 0) {
    Write-Host "Нашлись проблемы:" -ForegroundColor Red
    foreach ($problem in $problems) {
        Write-Host "  $problem" -ForegroundColor Red
    }
    exit 1
}

Write-Host "Всё работает. Строки для .env бота:"
Write-Host "OLLAMA_URL=http://${ip}:$port"
Write-Host "LLM_MODEL=$ChatModel"
Write-Host "EMBED_MODEL=$EmbedModel"
