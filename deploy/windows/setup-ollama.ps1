# Installs Ollama and Tailscale, removes Ollama from autostart and closes port 11434
# to everything except Tailscale. Models are downloaded later by pull-models.ps1,
# which runs without administrator rights.
# Needs administrator rights, the command that asks for them is in README.md.
# Safe to run again: what is already done is skipped or set to the same values.
param(
    [switch]$NoSleep,
    [string]$LogPath
)

. "$PSScriptRoot\common.ps1"

if ($LogPath) {
    Start-Transcript -Path $LogPath -Force | Out-Null
}

# Without this Russian text turns into garbage when another program (Codex) reads the output.
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
    Write-Host "Не получилось переключить вывод на UTF-8, русский текст может прийти кривым."
}

$tailscaleRange = "100.64.0.0/10"
# OLLAMA_HOST=0.0.0.0 makes Go open a dual-stack socket on Windows ([::]:11434),
# so the block has to cover all IPv6 as well, not only IPv4 outside Tailscale.
$otherAddresses = @(
    "0.0.0.0-100.63.255.255",
    "100.128.0.0-255.255.255.255",
    "::-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff"
)

function Fail($text) {
    Write-Host $text -ForegroundColor Red
    if ($LogPath) {
        Stop-Transcript | Out-Null
    }
    exit 1
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Install-WithWinget($id, $title) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail "Не нашёл winget, поэтому не могу поставить $title. Поставь 'Установщик приложений' из Microsoft Store или установи $title вручную, потом запусти скрипт снова."
    }
    Write-Host "Ставлю $title через winget, это займёт пару минут..."
    # Through Out-Host, otherwise Start-Transcript in PowerShell 5.1 does not see the output.
    winget install --id $id --exact --silent --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        Write-Host "winget вернул код $LASTEXITCODE, смотрю, встала ли программа." -ForegroundColor Yellow
    }
    Update-SessionPath
}

# Right after installation the adapter can take a few seconds to appear.
function Find-TailscaleAdapter {
    for ($i = 0; $i -lt 15; $i++) {
        $adapter = Get-NetAdapter -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq "Tailscale" -or $_.InterfaceDescription -like "Tailscale*" } |
            Select-Object -First 1
        if ($adapter) { return $adapter }
        Start-Sleep -Seconds 2
    }
    return $null
}

# Removing and creating again is the simplest way to get exactly these settings on every run.
function Reset-Rule($name, $description, $settings) {
    Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $name -Description $description -Direction Inbound -Profile Any -Protocol TCP -LocalPort $port @settings -ErrorAction Stop | Out-Null
}

if (-not (Test-Elevated)) {
    Fail "Нужны права администратора. Запусти скрипт командой из README (шаг 2): Windows спросит разрешение, человек нажмёт 'Да'."
}

# Ollama installs into the user's profile, and the firewall rules point at that ollama.exe.
# If UAC was confirmed with another account's password, both would end up in the wrong place.
$session = (Get-Process -Id $PID).SessionId
$explorer = Get-CimInstance Win32_Process -Filter "Name = 'explorer.exe' AND SessionId = $session" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($explorer) {
    $owner = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
    if ($owner.User -and $owner.User -ne $env:USERNAME) {
        Fail "Скрипт запущен под учётной записью $env:USERNAME, а в Windows вошёл $($owner.User). Запускать нужно под $($owner.User), и у этой учётной записи должны быть права администратора."
    }
}

$problem = Get-FirewallProblem
if ($problem) {
    Fail "$problem Ничего не менял. Когда исправишь, запусти скрипт снова."
}
Write-Host "Брандмауэр Windows включён для всех трёх профилей сети."

# Files copied from the internet carry a mark, and Windows would ask about the .vbs on every start.
Get-ChildItem -Path $PSScriptRoot -File | Unblock-File

$ollama = Find-Ollama
if ($ollama) {
    Write-Host "Ollama уже стоит: $ollama"
} else {
    Install-WithWinget "Ollama.Ollama" "Ollama"
    $ollama = Find-Ollama
    if (-not $ollama) {
        Fail "Ollama не нашлась после установки. Запусти скрипт ещё раз."
    }
}

$tailscale = Find-Tailscale
if ($tailscale) {
    Write-Host "Tailscale уже стоит: $tailscale"
} else {
    Install-WithWinget "Tailscale.Tailscale" "Tailscale"
    $tailscale = Find-Tailscale
    if (-not $tailscale) {
        Fail "Tailscale не нашёлся после установки. Запусти скрипт ещё раз."
    }
}

# The Ollama installer starts its tray app, and the app starts the server.
if (@(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue).Count -gt 0) {
    $left = @(Stop-Ollama)
    if ($left.Count -gt 0) {
        Fail "Не получилось остановить Ollama ($($left -join ', ')). Закрой её в диспетчере задач и запусти скрипт снова."
    }
    Write-Host "Остановил запущенную Ollama."
}

# The model has to run only when the owner turns it on, so Ollama must not start with Windows.
$startupFolders = @([Environment]::GetFolderPath("Startup"), [Environment]::GetFolderPath("CommonStartup"))
foreach ($folder in $startupFolders) {
    foreach ($link in @(Get-ChildItem -Path $folder -Filter "Ollama*.lnk" -ErrorAction SilentlyContinue)) {
        Remove-Item -Path $link.FullName -Force
        Write-Host "Убрал Ollama из автозагрузки: $($link.FullName)"
    }
}
foreach ($key in @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Run", "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run")) {
    $item = Get-Item -Path $key -ErrorAction SilentlyContinue
    if (-not $item) { continue }
    foreach ($name in $item.GetValueNames()) {
        $value = [string]$item.GetValue($name)
        if ($name -like "*ollama*" -or $value -like "*ollama*") {
            Remove-ItemProperty -Path $key -Name $name
            Write-Host "Убрал Ollama из автозагрузки: $key\$name"
        }
    }
}

# Older versions of this script set these for the whole machine. With them the tray app,
# if someone opens it, would listen on the network too.
foreach ($name in @("OLLAMA_HOST", "OLLAMA_KEEP_ALIVE")) {
    foreach ($scope in @("Machine", "User")) {
        $value = [Environment]::GetEnvironmentVariable($name, $scope)
        if ($value) {
            [Environment]::SetEnvironmentVariable($name, $null, $scope)
            Write-Host "Убрал переменную Windows $name ($scope), там было '$value'."
        }
    }
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}

# When the user presses Cancel in the firewall prompt, Windows adds its own block for ollama.exe.
# It would also block Tailscale, and our rules below already block everything else.
$appFilters = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program -like "*\ollama.exe" })
foreach ($filter in $appFilters) {
    $rule = $filter | Get-NetFirewallRule -ErrorAction SilentlyContinue
    if (-not $rule) { continue }
    if ([string]$rule.Action -eq "Block" -and [string]$rule.Direction -eq "Inbound" -and $firewallRules -notcontains $rule.DisplayName) {
        Remove-NetFirewallRule -Name $rule.Name
        Write-Host "Убрал правило брандмауэра '$($rule.DisplayName)': оно запрещало ollama.exe и Tailscale тоже."
    }
}

$adapter = Find-TailscaleAdapter
$blockSettings = @{ Action = "Block"; RemoteAddress = $otherAddresses }
$allowSettings = @{ Action = "Allow"; RemoteAddress = $tailscaleRange }
if ($adapter) {
    $allowSettings.InterfaceAlias = $adapter.Name
}

# The ollama.exe rules repeat the port rules for the program itself. With a rule for the program
# Windows has nothing to ask about when Ollama starts listening, so there is no firewall prompt.
try {
    Reset-Rule $portBlockRule "Корсик: порт модели закрыт для всех, кроме сети Tailscale" $blockSettings
    Reset-Rule $appBlockRule "Корсик: ollama.exe закрыта для всех, кроме сети Tailscale" ($blockSettings + @{ Program = $ollama })
    Reset-Rule $portAllowRule "Корсик: порт модели открыт только для сети Tailscale" $allowSettings
    Reset-Rule $appAllowRule "Корсик: ollama.exe открыта только для сети Tailscale" ($allowSettings + @{ Program = $ollama })
} catch {
    Fail "Не получилось настроить брандмауэр: $($_.Exception.Message)"
}

$stored = @((Get-NetFirewallRule -DisplayName $portBlockRule | Get-NetFirewallAddressFilter).RemoteAddress)
Write-Host "Порт $port закрыт для адресов: $($stored -join ', ')"
if (-not ($stored -like "*:*")) {
    Fail "Windows не сохранил запрет для IPv6, по IPv6 порт остался бы открыт. Пришли этот вывод Степану."
}
if ($adapter) {
    Write-Host "Порт $port открыт только для $tailscaleRange и только через адаптер '$($adapter.Name)'."
} else {
    Write-Host "Адаптер Tailscale не нашёлся, поэтому порт $port открыт для $tailscaleRange на любом адаптере. После входа в Tailscale запусти скрипт ещё раз, он привяжет правило к адаптеру." -ForegroundColor Yellow
}

if ($NoSleep) {
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
    Write-Host "Сон и гибернация при питании от сети отключены."
}

# The Ollama installer can start its app a little late. Everything started from here runs
# as administrator, and the window, which runs as a normal user, could not stop it.
$left = @(Stop-Ollama)
if ($left.Count -gt 0) {
    Fail "Не получилось остановить Ollama ($($left -join ', ')). Закрой её в диспетчере задач и запусти скрипт снова."
}

$ip = Get-TailscaleIp $tailscale
Write-Host ""
Write-Host "Установка закончена, Ollama не запущена."
if ($ip) {
    Write-Host "Tailscale подключен, адрес этого компьютера: $ip"
} else {
    Write-Host "Tailscale ещё не подключен. Человек должен войти: значок Tailscale возле часов, Log in, аккаунт команды." -ForegroundColor Yellow
}
Write-Host "Дальше без прав администратора: pull-models.ps1 скачает модели и положит на рабочий стол ярлык 'Корсик модель'."

if ($LogPath) {
    Stop-Transcript | Out-Null
}
