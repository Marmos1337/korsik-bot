# Window with one button that turns the model for the bot on and off.
# Opened by the "Корсик модель" desktop shortcut through korsik-model.vbs, so there is no console.
# The shortcut runs it as a normal user, so Ollama started from here has no administrator rights.
param(
    [string]$ChatModel = "qwen3:14b",
    [string]$EmbedModel = "bge-m3"
)

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
. "$PSScriptRoot\common.ps1"

$title = "Корсик: модель"

# A second window would fight the first one over the same Ollama processes.
$mutex = New-Object System.Threading.Mutex($false, "Local\KorsikModelWindow")
try {
    $owned = $mutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # The previous window was killed without closing, the mutex is ours now.
    $owned = $true
}
if (-not $owned) {
    [System.Windows.Forms.MessageBox]::Show("Окно уже открыто.", $title) | Out-Null
    exit
}

$shared = [hashtable]::Synchronized(@{
    State = "checking"
    Busy = $true
    Command = $null
    Note = ""
    Models = ""
    Gpu = ""
    Ip = ""
    CloseAfterStop = $false
    CloseNow = $false
    Quit = $false
    Log = [System.Collections.ArrayList]::Synchronized((New-Object System.Collections.ArrayList))
})
$settings = @{
    Folder = $PSScriptRoot
    ChatModel = $ChatModel
    EmbedModel = $EmbedModel
}

# Everything slow (HTTP, nvidia-smi, tailscale, firewall) runs in a separate runspace,
# so the window never freezes. The window only reads $shared on a timer.
$workerScript = {
    . (Join-Path $settings.Folder "common.ps1")
    $ProgressPreference = "SilentlyContinue"
    $chatModel = $settings.ChatModel
    $embedModel = $settings.EmbedModel
    $ollama = Find-Ollama
    $tailscale = Find-Tailscale

    function Add-Log($text) {
        [void]$shared.Log.Add((Get-Date -Format "HH:mm:ss") + "  " + $text)
    }

    function Set-Error($text) {
        $shared.State = "error"
        $shared.Note = $text
        Add-Log $text
    }

    function Get-GpuMemory {
        $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $smi) { return "нет данных, nvidia-smi не найден" }
        $output = & $smi.Source "--query-gpu=memory.used,memory.total" "--format=csv,noheader,nounits" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { return "нет данных" }
        $parts = ([string]($output | Select-Object -First 1)).Split(",")
        return "занято $($parts[0].Trim()) из $($parts[1].Trim()) МБ"
    }

    function Update-Status([switch]$Initial) {
        $up = Test-OllamaUp
        $models = "не загружены"
        if ($up) {
            $lines = @()
            foreach ($model in (Invoke-RestMethod -Uri "$localUrl/api/ps" -TimeoutSec 3).models) {
                $lines += Format-LoadedModel $model
            }
            if ($lines.Count -gt 0) { $models = $lines -join "`r`n" }
        }
        $shared.Models = $models
        $shared.Gpu = Get-GpuMemory
        $shared.Ip = Get-TailscaleIp $tailscale

        if ($shared.Busy -and -not $Initial) { return }
        $state = $shared.State
        if ($up) {
            if ($state -ne "running") {
                $shared.State = "running"
                if (Test-WideListener) {
                    $shared.Note = "Модель включена."
                } else {
                    $shared.Note = "Ollama запущена не из этого окна и слушает только этот компьютер, бот её не видит. Нажми «Выключить», потом «Включить»."
                }
                Add-Log $shared.Note
            }
        } elseif ($state -eq "running") {
            Set-Error "Ollama перестала отвечать. Нажми «Включить», чтобы запустить её заново."
        } elseif ($state -ne "error") {
            $shared.State = "off"
            $shared.Note = "Видеокарта свободна, бот работает в упрощённом режиме."
        }
    }

    function Start-Model {
        $shared.State = "starting"
        $shared.Note = "Это займёт до минуты."
        Add-Log "Включаю модель."

        # Ollama is about to listen on all addresses, only the firewall keeps the local network out.
        $problem = Get-FirewallProblem -WithRules
        if ($problem) {
            Set-Error "Не включаю: $problem"
            return
        }
        if (-not $ollama) {
            Set-Error "Не нашёл ollama.exe. Запусти setup-ollama.ps1."
            return
        }
        if (-not (Get-TailscaleIp $tailscale)) {
            Add-Log "Tailscale не подключен, бот модель не увидит. Открой Tailscale возле часов и войди."
        }

        $left = @(Stop-Ollama)
        if ($left.Count -gt 0) {
            Set-Error "Не получилось закрыть старые процессы Ollama. Скорее всего, их запустили от имени администратора: закрой их в диспетчере задач."
            return
        }

        $server = Start-OllamaServer $ollama "0.0.0.0:$port"
        if (-not (Wait-Ollama $server 60)) {
            $null = Stop-Ollama
            Set-Error "Ollama не запустилась. Последние строки её журнала: $(Get-ServerLogTail)"
            return
        }
        Add-Log "Ollama запущена."

        $missing = @(Get-MissingModels @($chatModel, $embedModel))
        if ($missing.Count -gt 0) {
            $null = Stop-Ollama
            Set-Error "На компьютере нет модели $($missing -join ', '). Запусти setup-ollama.ps1, он её скачает."
            return
        }

        try {
            Add-Log "Загружаю $chatModel в видеокарту."
            $null = Invoke-OllamaPost "/api/generate" @{ model = $chatModel; keep_alive = -1 } 600
            Add-Log "Загружаю $embedModel."
            $null = Invoke-OllamaPost "/api/embed" @{ model = $embedModel; input = "test"; keep_alive = -1 } 600
        } catch {
            $null = Stop-Ollama
            Set-Error "Модель не загрузилась: $($_.Exception.Message)"
            return
        }

        foreach ($model in (Invoke-RestMethod -Uri "$localUrl/api/ps" -TimeoutSec 5).models) {
            if ($model.size_vram -lt $model.size) {
                Add-Log "$($model.name) не поместилась в видеокарту целиком, ответы будут медленнее. Если так каждый раз, возьми qwen3:8b (README, «Какую модель брать»)."
            }
        }
        $shared.State = "running"
        $shared.Note = "Бот подключится в течение минуты."
        Add-Log "Модель работает."
    }

    function Stop-Model {
        $shared.State = "stopping"
        $shared.Note = ""
        Add-Log "Выключаю модель."
        if (Test-OllamaUp) {
            try {
                $loaded = (Invoke-RestMethod -Uri "$localUrl/api/ps" -TimeoutSec 5).models
            } catch {
                $loaded = @()
                Add-Log "Не получил список моделей, их выгрузит остановка Ollama."
            }
            foreach ($model in $loaded) {
                try {
                    $null = Invoke-OllamaPost "/api/generate" @{ model = $model.name; keep_alive = 0 } 60
                } catch {
                    Add-Log "$($model.name) не выгрузилась сама, её выгрузит остановка Ollama."
                }
            }
        }
        $left = @(Stop-Ollama)
        if ($left.Count -gt 0) {
            Set-Error "Не получилось остановить процессы Ollama ($($left -join ', ')). Скорее всего, их запустили от имени администратора: закрой их в диспетчере задач."
            return $false
        }
        $shared.State = "off"
        $shared.Models = "не загружены"
        $shared.Note = "Видеокарта свободна, бот работает в упрощённом режиме."
        Add-Log "Модель выключена, все процессы Ollama остановлены."
        return $true
    }

    if (Test-Elevated) {
        Add-Log "Окно открыто с правами администратора, Ollama тоже получит эти права. Лучше открывать его ярлыком на рабочем столе."
    }
    try {
        Update-Status -Initial
    } catch {
        Set-Error "Не получилось проверить состояние: $($_.Exception.Message)"
    }
    $shared.Busy = $false

    $nextRefresh = (Get-Date).AddSeconds(5)
    while (-not $shared.Quit) {
        $command = $shared.Command
        if ($command) {
            $shared.Command = $null
            try {
                if ($command -eq "start") {
                    Start-Model
                } else {
                    $stopped = Stop-Model
                    if ($shared.CloseAfterStop) {
                        if ($stopped) {
                            $shared.CloseNow = $true
                        } else {
                            $shared.CloseAfterStop = $false
                        }
                    }
                }
            } catch {
                Set-Error "Что-то пошло не так: $($_.Exception.Message)"
                $shared.CloseAfterStop = $false
            }
            $shared.Busy = $false
            $nextRefresh = Get-Date
        }
        if ((Get-Date) -ge $nextRefresh) {
            try {
                Update-Status
            } catch {
                Add-Log "Не получилось обновить состояние: $($_.Exception.Message)"
            }
            $nextRefresh = (Get-Date).AddSeconds(5)
        }
        Start-Sleep -Milliseconds 200
    }
}

$runspace = [runspacefactory]::CreateRunspace()
$runspace.Open()
$runspace.SessionStateProxy.SetVariable("shared", $shared)
$runspace.SessionStateProxy.SetVariable("settings", $settings)
$worker = [powershell]::Create()
$worker.Runspace = $runspace
$null = $worker.AddScript($workerScript.ToString())
$job = $worker.BeginInvoke()

$stateTexts = @{
    checking = "Проверяю..."
    off = "Модель выключена"
    starting = "Модель запускается"
    running = "Модель работает"
    stopping = "Модель выключается"
    error = "Ошибка"
}

[System.Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object System.Windows.Forms.Form
$form.Text = $title
$form.StartPosition = "CenterScreen"
$form.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$form.ClientSize = New-Object System.Drawing.Size(480, 600)
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$ollamaExe = Find-Ollama
if ($ollamaExe) {
    $appExe = Join-Path (Split-Path $ollamaExe) "ollama app.exe"
    if (Test-Path $appExe) {
        $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($appExe)
    }
}

$layout = New-Object System.Windows.Forms.TableLayoutPanel
$layout.Dock = "Fill"
$layout.Padding = New-Object System.Windows.Forms.Padding(16)
$layout.ColumnCount = 2
[void]$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::AutoSize)))
[void]$layout.ColumnStyles.Add((New-Object System.Windows.Forms.ColumnStyle([System.Windows.Forms.SizeType]::Percent, 100)))
$form.Controls.Add($layout)

function New-Label($text, $width) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $text
    $label.AutoSize = $true
    $label.MaximumSize = New-Object System.Drawing.Size($width, 0)
    $label.Margin = New-Object System.Windows.Forms.Padding(0, 4, 0, 4)
    return $label
}

function Add-Row($control, $caption, [switch]$Fill) {
    $row = $layout.RowCount
    $layout.RowCount = $row + 1
    if ($Fill) {
        [void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::Percent, 100)))
    } else {
        [void]$layout.RowStyles.Add((New-Object System.Windows.Forms.RowStyle([System.Windows.Forms.SizeType]::AutoSize)))
    }
    if ($caption) {
        $label = New-Label $caption 120
        $label.ForeColor = [System.Drawing.Color]::DimGray
        $label.Margin = New-Object System.Windows.Forms.Padding(0, 4, 12, 4)
        $layout.Controls.Add($label, 0, $row)
        $layout.Controls.Add($control, 1, $row)
    } else {
        $layout.Controls.Add($control, 0, $row)
        $layout.SetColumnSpan($control, 2)
    }
}

$stateLabel = New-Label "" 440
$stateLabel.Font = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
Add-Row $stateLabel

$noteLabel = New-Label "" 440
$noteLabel.ForeColor = [System.Drawing.Color]::DimGray
Add-Row $noteLabel

$button = New-Object System.Windows.Forms.Button
$button.Text = "Включить"
$button.Height = 48
$button.Anchor = "Left, Right"
$button.Font = New-Object System.Drawing.Font("Segoe UI", 12, [System.Drawing.FontStyle]::Bold)
$button.Margin = New-Object System.Windows.Forms.Padding(0, 10, 0, 14)
$button.Enabled = $false
Add-Row $button

$modelsLabel = New-Label "" 320
Add-Row $modelsLabel "Модели"
$gpuLabel = New-Label "" 320
Add-Row $gpuLabel "Видеокарта"
$ipLabel = New-Label "" 320
Add-Row $ipLabel "Tailscale"

$envCaption = New-Label "Строки для .env бота:" 440
$envCaption.ForeColor = [System.Drawing.Color]::DimGray
$envCaption.Margin = New-Object System.Windows.Forms.Padding(0, 12, 0, 4)
Add-Row $envCaption
$envBox = New-Object System.Windows.Forms.TextBox
$envBox.Multiline = $true
$envBox.ReadOnly = $true
$envBox.AutoSize = $false
$envBox.Height = 64
$envBox.Anchor = "Left, Right"
$envBox.Font = New-Object System.Drawing.Font("Consolas", 10)
Add-Row $envBox

$logCaption = New-Label "Журнал:" 440
$logCaption.ForeColor = [System.Drawing.Color]::DimGray
$logCaption.Margin = New-Object System.Windows.Forms.Padding(0, 12, 0, 4)
Add-Row $logCaption
$logBox = New-Object System.Windows.Forms.TextBox
$logBox.Multiline = $true
$logBox.ReadOnly = $true
$logBox.ScrollBars = "Vertical"
$logBox.Dock = "Fill"
Add-Row $logBox -Fill

$shownLines = 0
$canClose = $false
$workerFailed = $false

function Set-Text($control, $text) {
    $text = [string]$text
    if ($control.Text -ne $text) {
        $control.Text = $text
    }
}

function Update-Window {
    if ($job.IsCompleted -and -not $script:workerFailed) {
        $script:workerFailed = $true
        $reason = $worker.InvocationStateInfo.Reason
        if ($reason) {
            $reason = $reason.Message
        } else {
            $reason = [string]($worker.Streams.Error | Select-Object -First 1)
        }
        $shared.State = "error"
        $shared.Note = "Фоновая часть окна остановилась ($reason). Закрой окно и открой его снова."
    }

    $state = $shared.State
    Set-Text $stateLabel $stateTexts[$state]
    if ($state -eq "running") {
        $stateLabel.ForeColor = [System.Drawing.Color]::ForestGreen
    } elseif ($state -eq "error") {
        $stateLabel.ForeColor = [System.Drawing.Color]::Firebrick
    } else {
        $stateLabel.ForeColor = [System.Drawing.SystemColors]::ControlText
    }
    Set-Text $noteLabel $shared.Note

    if ($state -eq "running" -or $state -eq "starting") {
        Set-Text $button "Выключить"
    } else {
        Set-Text $button "Включить"
    }
    $button.Enabled = -not ($shared.Busy -or $script:workerFailed)

    Set-Text $modelsLabel $shared.Models
    Set-Text $gpuLabel $shared.Gpu
    $ip = $shared.Ip
    if ($ip) {
        Set-Text $ipLabel $ip
        Set-Text $envBox "OLLAMA_URL=http://${ip}:$port`r`nLLM_MODEL=$ChatModel`r`nEMBED_MODEL=$EmbedModel"
    } else {
        Set-Text $ipLabel "не подключен"
        Set-Text $envBox "Появятся, когда Tailscale подключится."
    }

    while ($script:shownLines -lt $shared.Log.Count) {
        $logBox.AppendText($shared.Log[$script:shownLines] + "`r`n")
        $script:shownLines++
    }

    if ($shared.CloseNow) {
        $script:canClose = $true
        $timer.Stop()
        $form.Close()
    }
}

$button.Add_Click({
    if ($shared.Busy) { return }
    $shared.Busy = $true
    if ($shared.State -eq "running") {
        $shared.State = "stopping"
        $shared.Command = "stop"
    } else {
        $shared.State = "starting"
        $shared.Command = "start"
    }
    Update-Window
})

$form.Add_FormClosing({
    param($source, $closing)
    if ($script:canClose -or $closing.CloseReason -eq "WindowsShutDown") { return }
    if ($shared.Busy -and -not $script:workerFailed) {
        $closing.Cancel = $true
        [System.Windows.Forms.MessageBox]::Show("Подожди, модель ещё включается или выключается.", $title) | Out-Null
        return
    }
    if (@(Get-Process -Name "ollama*" -ErrorAction SilentlyContinue).Count -eq 0) { return }

    $answer = [System.Windows.Forms.MessageBox]::Show("Выключить модель и закрыть окно?", $title, "YesNo", "Question")
    if ([string]$answer -ne "Yes") {
        $closing.Cancel = $true
        return
    }
    if ($script:workerFailed) {
        # Nothing runs in the background any more, so the model is stopped right here.
        $null = Stop-Ollama
        return
    }
    $closing.Cancel = $true
    $shared.Busy = $true
    $shared.CloseAfterStop = $true
    $shared.State = "stopping"
    $shared.Command = "stop"
})

$form.Add_Shown({ $form.Activate() })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 300
$timer.Add_Tick({ Update-Window })
Update-Window
$timer.Start()

[void]$form.ShowDialog()

$shared.Quit = $true
$timer.Stop()
$mutex.ReleaseMutex()
