# Shared parts of setup-ollama.ps1, pull-models.ps1, korsik-model.ps1 and check-ollama.ps1.
# Loaded with ". .\common.ps1", it does nothing on its own.

$port = 11434
$localUrl = "http://127.0.0.1:$port"
$serverLog = Join-Path $env:LOCALAPPDATA "Ollama\korsik-serve.log"

$portBlockRule = "Ollama 11434 block others"
$portAllowRule = "Ollama 11434 allow Tailscale"
$appBlockRule = "ollama.exe block others"
$appAllowRule = "ollama.exe allow Tailscale"
$firewallRules = @($portBlockRule, $portAllowRule, $appBlockRule, $appAllowRule)

$profileNames = @{
    Domain = "доменной сети"
    Private = "частной сети"
    Public = "общедоступной сети"
}

function Find-Ollama {
    $path = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $path) { return $path }
    $command = Get-Command ollama.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

function Find-Tailscale {
    $path = "$env:ProgramFiles\Tailscale\tailscale.exe"
    if (Test-Path $path) { return $path }
    $command = Get-Command tailscale.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

function Get-TailscaleIp($tailscale) {
    if (-not $tailscale) { return $null }
    $output = & $tailscale ip -4 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $output) { return $null }
    return ([string]($output | Select-Object -First 1)).Trim()
}

function Test-Elevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# Returns $null when the firewall really filters the port, otherwise the reason in Russian.
# Rules are useless if a profile is off (a third-party firewall turns them off too)
# or if group policy tells Windows to ignore local rules.
function Get-FirewallProblem([switch]$WithRules) {
    try {
        $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    } catch {
        return "Не получилось прочитать настройки брандмауэра Windows: $($_.Exception.Message)"
    }
    if ($profiles.Count -lt 3) {
        return "Брандмауэр Windows вернул не все профили сети, проверить его не получилось."
    }
    foreach ($item in $profiles) {
        $where = $profileNames[[string]$item.Name]
        if ([string]$item.Enabled -ne "True") {
            return "Брандмауэр Windows выключен для $where, без него порт модели открыт всей локальной сети. Включи его: Параметры, Безопасность Windows, Брандмауэр и защита сети. Если там написано, что защитой управляет другая программа, правила Windows не действуют."
        }
        if ([string]$item.AllowLocalFirewallRules -eq "False") {
            return "Групповая политика запрещает локальные правила брандмауэра для $where, правила для порта модели не действуют. Это решает администратор домена."
        }
    }
    if ($WithRules) {
        $found = @(Get-NetFirewallRule -DisplayName $firewallRules -ErrorAction SilentlyContinue)
        foreach ($name in $firewallRules) {
            $rule = $found | Where-Object { $_.DisplayName -eq $name -and [string]$_.Enabled -eq "True" } | Select-Object -First 1
            if (-not $rule) {
                return "Нет правила брандмауэра '$name' или оно выключено. Запусти setup-ollama.ps1."
            }
        }
    }
    return $null
}

function Test-OllamaUp {
    try {
        Invoke-RestMethod -Uri "$localUrl/api/version" -TimeoutSec 2 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Wait-Ollama($process, $seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        if (Test-OllamaUp) { return $true }
        if ($process -and $process.HasExited) { return $false }
        Start-Sleep -Seconds 1
    }
    return $false
}

# The variables go only to this process. Windows itself has no OLLAMA_HOST,
# so Ollama started any other way listens on 127.0.0.1 only.
function Start-OllamaServer($ollama, $listen) {
    $folder = Split-Path $serverLog
    if (-not (Test-Path $folder)) {
        New-Item -ItemType Directory -Path $folder -Force | Out-Null
    }
    $info = New-Object System.Diagnostics.ProcessStartInfo
    # cmd is here only to write the Ollama log to a file.
    $info.FileName = "$env:SystemRoot\System32\cmd.exe"
    $info.Arguments = "/d /c `"`"$ollama`" serve > `"$serverLog`" 2>&1`""
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.EnvironmentVariables["OLLAMA_HOST"] = $listen
    $info.EnvironmentVariables["OLLAMA_KEEP_ALIVE"] = "-1"
    return [System.Diagnostics.Process]::Start($info)
}

# Returns the names of processes that are still alive, nothing if all stopped.
# The tray app restarts a killed server, so everything named ollama* goes, a few times if needed.
function Stop-Ollama {
    for ($i = 0; $i -lt 20; $i++) {
        $running = @(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue)
        if ($running.Count -eq 0) { return }
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 500
    }
    Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | ForEach-Object { $_.ProcessName }
}

function Get-ServerLogTail {
    $lines = Get-Content -Path $serverLog -Tail 3 -ErrorAction SilentlyContinue
    if (-not $lines) { return "журнал пуст" }
    return ($lines -join " ")
}

# Windows PowerShell 5.1 breaks Cyrillic when the response has no charset, so bytes are decoded by hand.
function Invoke-OllamaPost($path, $body, $timeout) {
    $json = ConvertTo-Json -InputObject $body -Depth 5 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $response = Invoke-WebRequest -Uri "$localUrl$path" -Method Post -Body $bytes -ContentType "application/json; charset=utf-8" -UseBasicParsing -TimeoutSec $timeout
    $text = [System.Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray())
    return $text | ConvertFrom-Json
}

function Get-MissingModels($models) {
    $tags = Invoke-RestMethod -Uri "$localUrl/api/tags" -TimeoutSec 10
    $names = @($tags.models | ForEach-Object { $_.name })
    foreach ($model in $models) {
        if (($names -notcontains $model) -and ($names -notcontains "${model}:latest")) {
            $model
        }
    }
}

function Format-LoadedModel($model) {
    $total = [math]::Round($model.size / 1GB, 1)
    if ($model.size_vram -ge $model.size) {
        return "$($model.name): $total ГБ, вся в видеокарте"
    }
    $inGpu = [math]::Round($model.size_vram / 1GB, 1)
    return "$($model.name): $inGpu из $total ГБ в видеокарте, остальное в оперативной памяти"
}

# A dual-stack socket shows up as "::", a plain IPv4 one as "0.0.0.0".
function Test-WideListener {
    $listeners = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
    foreach ($item in $listeners) {
        if ($item.LocalAddress -eq "0.0.0.0" -or $item.LocalAddress -eq "::") { return $true }
    }
    return $false
}
