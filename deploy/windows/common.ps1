# Shared parts of setup-ollama.ps1, pull-models.ps1, korsik-model.ps1 and check-ollama.ps1.
# Loaded with ". .\common.ps1", it does nothing on its own.

$port = 11434
$localUrl = "http://127.0.0.1:$port"
$serverLog = Join-Path $env:LOCALAPPDATA "Ollama\korsik-serve.log"

# setup-ollama.ps1 writes here the addresses that may reach the model (the bot's server),
# the window and check-ollama.ps1 read them to know what the rules should look like.
$allowFromFile = Join-Path $PSScriptRoot "allow-from.txt"

$portBlockRule = "Ollama 11434 block others"
$portAllowRule = "Ollama 11434 allow server"
$appBlockRule = "ollama.exe block others"
$appAllowRule = "ollama.exe allow server"
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

# The IPv4 address of the adapter that holds the default route, that is the one the server sees.
function Get-LanIp {
    $route = Get-NetRoute -DestinationPrefix "0.0.0.0/0" -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Sort-Object -Property { $_.RouteMetric + $_.InterfaceMetric } |
        Select-Object -First 1
    if (-not $route) { return $null }
    $address = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -notlike "169.254.*" } |
        Select-Object -First 1
    if (-not $address) { return $null }
    return $address.IPAddress
}

function Get-AllowFrom {
    if (-not (Test-Path $allowFromFile)) { return }
    foreach ($line in Get-Content -Path $allowFromFile -Encoding UTF8) {
        $line = $line.Trim()
        if ($line -and -not $line.StartsWith("#")) { $line }
    }
}

function Test-Ipv4($text) {
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($text, [ref]$ip)) { return $false }
    # TryParse also takes things like "60" or "1.60", so the four parts are checked too.
    return ($ip.AddressFamily -eq "InterNetwork") -and ($text.Split(".").Count -eq 4)
}

function ConvertTo-Number($ip) {
    $bytes = ([System.Net.IPAddress]::Parse($ip)).GetAddressBytes()
    return ([int64]$bytes[0] -shl 24) + ([int64]$bytes[1] -shl 16) + ([int64]$bytes[2] -shl 8) + [int64]$bytes[3]
}

function ConvertTo-Ip($number) {
    return "{0}.{1}.{2}.{3}" -f (($number -shr 24) -band 255), (($number -shr 16) -band 255), (($number -shr 8) -band 255), ($number -band 255)
}

function Format-Range($first, $last) {
    if ($first -eq $last) { return (ConvertTo-Ip $first) }
    return "$(ConvertTo-Ip $first)-$(ConvertTo-Ip $last)"
}

# Everything except the allowed addresses: IPv4 ranges around them and all of IPv6.
# OLLAMA_HOST=0.0.0.0 makes Go open a dual-stack socket on Windows ([::]:11434),
# so without the IPv6 part the port would stay open over IPv6.
function Get-BlockedAddresses($allowFrom) {
    $numbers = @($allowFrom | ForEach-Object { ConvertTo-Number $_ } | Sort-Object -Unique)
    $ranges = @()
    $next = [int64]0
    foreach ($number in $numbers) {
        if ($number -gt $next) {
            $ranges += Format-Range $next ($number - 1)
        }
        $next = $number + 1
    }
    if ($next -le 4294967295) {
        $ranges += Format-Range $next 4294967295
    }
    $ranges += "::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
    return $ranges
}

# Windows may write an address differently from how it was given (IPv6 especially),
# so both sides are parsed and printed the same way before comparing.
function Format-Address($entry) {
    $entry = ([string]$entry) -replace '/(32|255\.255\.255\.255)$', ''
    $parts = foreach ($part in $entry.Split("-")) {
        $ip = $null
        if ([System.Net.IPAddress]::TryParse($part.Trim(), [ref]$ip)) {
            $ip.ToString()
        } else {
            $part.Trim().ToLower()
        }
    }
    return ($parts -join "-")
}

function Test-SameAddresses($stored, $expected) {
    $left = @($stored | ForEach-Object { Format-Address $_ } | Sort-Object) -join ","
    $right = @($expected | ForEach-Object { Format-Address $_ } | Sort-Object) -join ","
    return $left -eq $right
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
        $allowFrom = @(Get-AllowFrom)
        if ($allowFrom.Count -eq 0) {
            return "Нет списка адресов, которым открыт порт ($allowFromFile). Запусти setup-ollama.ps1."
        }
        $blocked = @(Get-BlockedAddresses $allowFrom)
        $expected = @{
            $portBlockRule = $blocked
            $appBlockRule = $blocked
            $portAllowRule = $allowFrom
            $appAllowRule = $allowFrom
        }
        $found = @(Get-NetFirewallRule -DisplayName $firewallRules -ErrorAction SilentlyContinue)
        foreach ($name in $firewallRules) {
            $rule = $found | Where-Object { $_.DisplayName -eq $name -and [string]$_.Enabled -eq "True" } | Select-Object -First 1
            if (-not $rule) {
                return "Нет правила брандмауэра '$name' или оно выключено. Запусти setup-ollama.ps1."
            }
            $stored = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
            if (-not (Test-SameAddresses $stored $expected[$name])) {
                return "Правило брандмауэра '$name' пускает не те адреса, что записаны в $allowFromFile. Запусти setup-ollama.ps1."
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
