# Installs Ollama, removes it from autostart and opens port 11434 only to the bot's server
# (-AllowFrom, one or more IPv4 addresses separated by commas). Models are downloaded later
# by pull-models.ps1, which runs without administrator rights.
# Needs administrator rights, the command that asks for them is in README.md.
# Safe to run again: what is already done is skipped or set to the same values.
param(
    [string]$AllowFrom = "192.168.1.60",
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

# Rules from the version that worked through Tailscale.
$oldRules = @("Ollama 11434 allow Tailscale", "ollama.exe allow Tailscale")

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

# Removing and creating again is the simplest way to get exactly these settings on every run.
function Reset-Rule($name, $description, $settings) {
    Remove-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    New-NetFirewallRule -DisplayName $name -Description $description -Direction Inbound -Profile Any -Protocol TCP -LocalPort $port @settings -ErrorAction Stop | Out-Null
}

# With -File PowerShell passes "a,b" as one string, so the list is split here.
$allowList = @($AllowFrom -split "[,;\s]+" | Where-Object { $_ } | Select-Object -Unique)
if ($allowList.Count -eq 0) {
    Fail "Не задан ни один адрес в -AllowFrom."
}
foreach ($address in $allowList) {
    if (-not (Test-Ipv4 $address)) {
        Fail "'$address' в -AllowFrom не похож на IPv4-адрес. Пример: -AllowFrom 192.168.1.60"
    }
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
# It would also block the server, and our rules below already block everything else.
$appFilters = @(Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue | Where-Object { $_.Program -like "*\ollama.exe" })
foreach ($filter in $appFilters) {
    $rule = $filter | Get-NetFirewallRule -ErrorAction SilentlyContinue
    if (-not $rule) { continue }
    if ([string]$rule.Action -eq "Block" -and [string]$rule.Direction -eq "Inbound" -and $firewallRules -notcontains $rule.DisplayName) {
        Remove-NetFirewallRule -Name $rule.Name
        Write-Host "Убрал правило брандмауэра '$($rule.DisplayName)': оно запрещало ollama.exe и для сервера тоже."
    }
}

foreach ($name in $oldRules) {
    if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
        Remove-NetFirewallRule -DisplayName $name
        Write-Host "Убрал старое правило брандмауэра '$name'."
    }
}

$blocked = @(Get-BlockedAddresses $allowList)
$blockSettings = @{ Action = "Block"; RemoteAddress = $blocked }
$allowSettings = @{ Action = "Allow"; RemoteAddress = $allowList }

# The ollama.exe rules repeat the port rules for the program itself. With a rule for the program
# Windows has nothing to ask about when Ollama starts listening, so there is no firewall prompt.
try {
    Reset-Rule $portBlockRule "Корсик: порт модели закрыт для всех, кроме сервера бота" $blockSettings
    Reset-Rule $appBlockRule "Корсик: ollama.exe закрыта для всех, кроме сервера бота" ($blockSettings + @{ Program = $ollama })
    Reset-Rule $portAllowRule "Корсик: порт модели открыт только для сервера бота" $allowSettings
    Reset-Rule $appAllowRule "Корсик: ollama.exe открыта только для сервера бота" ($allowSettings + @{ Program = $ollama })
} catch {
    Fail "Не получилось настроить брандмауэр: $($_.Exception.Message)"
}

# Windows may silently drop or change an address it did not like, so read back what it saved.
$expected = @{
    $portBlockRule = $blocked
    $appBlockRule = $blocked
    $portAllowRule = $allowList
    $appAllowRule = $allowList
}
foreach ($name in $firewallRules) {
    $stored = @((Get-NetFirewallRule -DisplayName $name | Get-NetFirewallAddressFilter).RemoteAddress)
    Write-Host "Правило '$name': $($stored -join ', ')"
    if (-not (Test-SameAddresses $stored $expected[$name])) {
        Fail "Windows сохранила в правиле '$name' не те адреса. Ждали: $($expected[$name] -join ', '). Пришли этот журнал Степану."
    }
}

Set-Content -Path $allowFromFile -Encoding UTF8 -Value (@("# Адреса, которым открыт порт $port. Менять через setup-ollama.ps1 -AllowFrom.") + $allowList)
Write-Host "Порт $port открыт только для: $($allowList -join ', '). Весь остальной IPv4 и весь IPv6 закрыты."

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

$ip = Get-LanIp
Write-Host ""
Write-Host "Установка закончена, Ollama не запущена."
if ($ip) {
    Write-Host "Адрес этого компьютера в локальной сети: $ip. Для бота: OLLAMA_URL=http://${ip}:$port"
} else {
    Write-Host "Не нашёл адрес этого компьютера в локальной сети: нет подключения с маршрутом по умолчанию." -ForegroundColor Yellow
}
Write-Host "Дальше без прав администратора: pull-models.ps1 скачает модели и положит на рабочий стол ярлык 'Корсик модель'."

if ($LogPath) {
    Stop-Transcript | Out-Null
}
