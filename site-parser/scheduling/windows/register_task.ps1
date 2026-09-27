<#
.SYNOPSIS
    Регистрирует задачу Планировщика Windows: прогон парсера по расписанию.

.DESCRIPTION
    Задача запускает `.venv\Scripts\python.exe -m site_parser run <конфиг>` из папки проекта.
    Журнал — logs\site_parser.log (ротация), код выхода виден в колонке «Результат последнего запуска»:
    0 — успешно, 1 — прогон не удался, 2 — ошибка конфига, 3 — готово с предупреждениями.

    Если компьютер был выключен в момент запуска, задача выполнится при включении (StartWhenAvailable).
    Второй экземпляр не запускается, пока идёт первый (MultipleInstances IgnoreNew).
    Права администратора не нужны: задача создаётся для текущего пользователя и работает, пока он вошёл
    в систему. Чтобы работала и без входа, см. параметр -RunWithoutLogon.

.EXAMPLE
    # Каждый день в 09:00
    .\scheduling\windows\register_task.ps1 -Config configs\books_toscrape.yaml -At 09:00

.EXAMPLE
    # Каждые 4 часа, начиная с 08:00
    .\scheduling\windows\register_task.ps1 -Config configs\books_toscrape.yaml -At 08:00 -EveryHours 4

.EXAMPLE
    # Посмотреть, что будет сделано, ничего не меняя
    .\scheduling\windows\register_task.ps1 -Config configs\books_toscrape.yaml -WhatIf

.EXAMPLE
    # Удалить задачу
    .\scheduling\windows\register_task.ps1 -Config configs\books_toscrape.yaml -Unregister
#>
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)]
    [string]$Config,

    # Время первого запуска, ЧЧ:ММ
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$At = "09:00",

    # Повторять каждые N часов (0 — раз в сутки)
    [ValidateRange(0, 23)]
    [int]$EveryHours = 0,

    # Имя задачи; по умолчанию "site-parser <имя конфига>"
    [string]$TaskName = "",

    # Дополнительные аргументы прогона, например "--max-pages 5"
    [string]$ExtraArgs = "",

    # Запускать, даже если пользователь не вошёл в систему (нужны права администратора;
    # Windows запросит пароль учётной записи при регистрации — вводите его сами)
    [switch]$RunWithoutLogon,

    [switch]$Unregister
)

$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ConfigPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $ProjectRoot $Config }
$ConfigName = [System.IO.Path]::GetFileNameWithoutExtension($ConfigPath)
if (-not $TaskName) { $TaskName = "site-parser $ConfigName" }

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        if ($PSCmdlet.ShouldProcess($TaskName, "Удалить задачу Планировщика")) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Задача «$TaskName» удалена."
        }
    } else {
        Write-Host "Задачи «$TaskName» нет."
    }
    return
}

if (-not (Test-Path $Python)) {
    throw "Не найдено виртуальное окружение: $Python. Сначала: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path $ConfigPath)) {
    throw "Не найден конфиг: $ConfigPath"
}

# Проверяем конфиг до регистрации: ошибка всплывёт сейчас, а не ночью в журнале.
& $Python -c "import sys; from site_parser.config import load_config; load_config(sys.argv[1])" $ConfigPath
if ($LASTEXITCODE -ne 0) { throw "Конфиг не прошёл проверку — задача не зарегистрирована." }

$Arguments = "-m site_parser run `"$ConfigPath`""
if ($ExtraArgs) { $Arguments = "$Arguments $ExtraArgs" }

$Action = New-ScheduledTaskAction -Execute $Python -Argument $Arguments -WorkingDirectory $ProjectRoot

$StartAt = [datetime]::ParseExact($At, "H:mm", $null)
if ($StartAt -lt (Get-Date)) { $StartAt = $StartAt.AddDays(1) }
if ($EveryHours -gt 0) {
    $Trigger = New-ScheduledTaskTrigger -Once -At $StartAt `
        -RepetitionInterval (New-TimeSpan -Hours $EveryHours) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $Schedule = "каждые $EveryHours ч, начиная с $($StartAt.ToString('dd.MM.yyyy HH:mm'))"
} else {
    $Trigger = New-ScheduledTaskTrigger -Daily -At $StartAt
    $Schedule = "ежедневно в $($StartAt.ToString('HH:mm'))"
}

$Settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)

$User = "$env:USERDOMAIN\$env:USERNAME"
$LogonType = if ($RunWithoutLogon) { "Password" } else { "Interactive" }
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType $LogonType -RunLevel Limited

$Task = New-ScheduledTask -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal `
    -Description "Парсер сайта по конфигу $ConfigName ($Schedule). Проект: $ProjectRoot"

if ($PSCmdlet.ShouldProcess($TaskName, "Зарегистрировать задачу ($Schedule)")) {
    if ($RunWithoutLogon) {
        $Credential = Get-Credential -UserName $User -Message "Пароль учётной записи для задачи «$TaskName»"
        Register-ScheduledTask -TaskName $TaskName -InputObject $Task -User $User `
            -Password $Credential.GetNetworkCredential().Password -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $TaskName -InputObject $Task -Force | Out-Null
    }
    Write-Host "Задача «$TaskName» зарегистрирована: $Schedule."
    Write-Host "Запустить сейчас:  Start-ScheduledTask -TaskName '$TaskName'"
    Write-Host "Журнал:            $ProjectRoot\logs\site_parser.log"
}
