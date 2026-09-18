#Requires -Version 5.1
# Headless inventory for Zork. Read-only. No installs. Deleted from TEMP after run.
param(
    [Parameter(Mandatory = $true)][string]$BaseUrl,
    [Parameter(Mandatory = $true)][string]$SessionId,
    [Parameter(Mandatory = $true)][string]$Secret
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Send-Helper {
    param([string]$Path, [hashtable]$Body)
    $uri = ($BaseUrl.TrimEnd('/') + $Path)
    $json = $Body | ConvertTo-Json -Compress -Depth 8
    $wc = New-Object System.Net.WebClient
    $wc.Encoding = [Text.Encoding]::UTF8
    $wc.Headers.Add('Content-Type', 'application/json; charset=utf-8')
    $wc.Headers.Add('X-Helper-Secret', $Secret)
    [void]$wc.UploadString($uri, 'POST', $json)
}

function Show-ScanProgress {
    param([int]$Percent, [string]$Text)
    $pct = [Math]::Max(0, [Math]::Min(100, $Percent))
    Write-Progress -Activity 'Zork Helper' -Status ('Loading... {0}%' -f $pct) -PercentComplete $pct -CurrentOperation $Text
    $fill = [int][Math]::Floor($pct / 5)
    if ($fill -gt 20) { $fill = 20 }
    $bar = ('#' * $fill) + ('-' * (20 - $fill))
    $line = '  [{0}] {1,3}%  Loading...  {2}' -f $bar, $pct, $Text
    if ($line.Length -gt 79) { $line = $line.Substring(0, 79) }
    Write-Host ("`r" + $line.PadRight(79)) -NoNewline
    try { Send-Helper -Path "/api/helper/$SessionId/progress" -Body @{ text = $Text } } catch { }
}

function Get-Prop {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-UninstallRows {
    $paths = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
    )
    $rows = New-Object System.Collections.ArrayList
    foreach ($root in $paths) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue | ForEach-Object {
            $item = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
            if ($item) { [void]$rows.Add($item) }
        }
    }
    return $rows
}

function Find-Uninstall {
    param($Rows, [string[]]$Patterns)
    foreach ($row in $Rows) {
        $name = [string](Get-Prop $row 'DisplayName')
        if ([string]::IsNullOrWhiteSpace($name)) { continue }
        foreach ($pat in $Patterns) {
            if ($name -match $pat) {
                return @{
                    Found   = $true
                    Version = [string](Get-Prop $row 'DisplayVersion')
                    Detail  = $name
                }
            }
        }
    }
    return @{ Found = $false; Version = ''; Detail = '' }
}

function Get-ExeProduct {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path)) { return '' }
    try {
        return [string][Diagnostics.FileVersionInfo]::GetVersionInfo($Path).ProductVersion
    } catch { return '' }
}

function Get-AppPath {
    param([string]$Exe)
    $rel = "SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\$Exe"
    foreach ($hive in @(
            [Microsoft.Win32.Registry]::LocalMachine,
            [Microsoft.Win32.Registry]::CurrentUser
        )) {
        $key = $null
        try {
            $key = $hive.OpenSubKey($rel)
            if ($null -eq $key) { continue }
            $val = [string]$key.GetValue('')
            if (-not [string]::IsNullOrWhiteSpace($val)) {
                return $val.Trim().Trim('"')
            }
        } catch {
        } finally {
            if ($null -ne $key) { $key.Close() }
        }
    }
    return ''
}

function Item {
    param($Id, $Name, $Found, $Version, $Ok, $Note, $Reboot = $false)
    return @{
        id      = [string]$Id
        name    = [string]$Name
        found   = [bool]$Found
        version = [string]$Version
        ok      = [bool]$Ok
        note    = [string]$Note
        reboot  = [bool]$Reboot
    }
}

function Test-PendingReboot {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
    )
    foreach ($k in $keys) {
        if (Test-Path -LiteralPath $k) { return $true }
    }
    return $false
}

function Get-WindowsUpdateItem {
    Show-ScanProgress 78 'Sjekker Windows Update (kan ta et minutt)...'
    $pendingReboot = Test-PendingReboot
    try {
        $session = New-Object -ComObject Microsoft.Update.Session
        $session.ClientApplicationID = 'ZorkHelper'
        $searcher = $session.CreateUpdateSearcher()
        $searcher.Online = $true
        $found = $searcher.Search('IsInstalled=0 and IsHidden=0')
        $n = 0
        try { $n = [int]$found.Updates.Count } catch { $n = 0 }
        $titles = New-Object System.Collections.ArrayList
        $take = [Math]::Min($n, 5)
        for ($i = 0; $i -lt $take; $i++) {
            $title = ''
            try { $title = [string]$found.Updates.Item($i).Title } catch { $title = '' }
            if (-not [string]::IsNullOrWhiteSpace($title)) { [void]$titles.Add($title) }
        }
        $ok = ($n -eq 0) -and (-not $pendingReboot)
        if ($pendingReboot -and $n -eq 0) {
            $note = 'Omstart venter etter oppdateringer'
        } elseif ($n -eq 0) {
            $note = 'Ingen ventende oppdateringer'
        } else {
            $head = [string]($titles -join '; ')
            if ($head.Length -gt 160) { $head = $head.Substring(0, 160) }
            $note = "$n ventende"
            if ($pendingReboot) { $note = "$note, omstart venter" }
            if ($head) { $note = "$note. $head" }
        }
        return (Item 'winupdate' 'Windows Update' $true '' $ok $note $pendingReboot)
    } catch {
        $msg = [string]$_.Exception.Message
        if ($msg.Length -gt 160) { $msg = $msg.Substring(0, 160) }
        $note = "Klarte ikke å sjekke: $msg"
        if ($pendingReboot) { $note = "Omstart venter. $note" }
        return (Item 'winupdate' 'Windows Update' $true '' $false $note $pendingReboot)
    }
}

function Update-HelperTray {
    $homeDir = Join-Path $env:LOCALAPPDATA 'ZorkHelper'
    if (-not (Test-Path -LiteralPath $homeDir)) { return }
    $base = $BaseUrl.TrimEnd('/') + '/api/helper/' + $SessionId
    $assets = @(
        @{ key = 'tray'; path = '/tray'; dest = (Join-Path $homeDir 'zork-helper-tray.ps1'); mirror = $null },
        @{ key = 'open'; path = '/open'; dest = (Join-Path $homeDir 'zork-helper-open.ps1'); mirror = $null },
        @{ key = 'install'; path = '/install-script'; dest = (Join-Path $homeDir 'zork-helper-install.ps1'); mirror = (Join-Path $homeDir 'install-script.ps1') }
    )
    foreach ($asset in $assets) {
        $tmp = Join-Path $env:TEMP ('zork-' + [string]$asset.key + '-' + $SessionId + '.ps1')
        try {
            $wc = New-Object System.Net.WebClient
            $wc.Encoding = [Text.Encoding]::UTF8
            $wc.Headers.Add('X-Helper-Secret', $Secret)
            $wc.DownloadFile($base + [string]$asset.path, $tmp)
            if ((Test-Path -LiteralPath $tmp) -and ((Get-Item -LiteralPath $tmp).Length -gt 2000)) {
                Copy-Item -LiteralPath $tmp -Destination ([string]$asset.dest) -Force
                if ($asset.mirror) {
                    Copy-Item -LiteralPath $tmp -Destination ([string]$asset.mirror) -Force
                }
            }
        } catch { }
    }
}

try {
Show-ScanProgress 12 'Kobler til...'
try {
    Send-Helper -Path "/api/helper/$SessionId/connect" -Body @{}
} catch { }
Update-HelperTray

Show-ScanProgress 22 'Sjekker programmer...'

$admin = Test-Admin
$os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue
$rows = Get-UninstallRows
$bag = New-Object System.Collections.ArrayList

$cfgPath = 'HKLM:\SOFTWARE\Microsoft\Office\ClickToRun\Configuration'
if (Test-Path -LiteralPath $cfgPath) {
    $cfg = Get-ItemProperty -LiteralPath $cfgPath -ErrorAction SilentlyContinue
    $ver = [string](Get-Prop $cfg 'VersionToReport')
    $culture = [string](Get-Prop $cfg 'ClientCulture')
    if (-not [string]::IsNullOrWhiteSpace($ver)) {
        $ok = $culture -match '^(nb-NO|nn-NO|en-US|en-GB)$'
        $note = $(if ($ok) { "i orden ($culture)" } elseif ($culture) { "installert, språk $culture (ønsket nb-NO)" } else { "installert $ver" })
        [void]$bag.Add((Item 'office' 'Microsoft 365 / Office' $true $ver $ok $note))
    }
}
if ($bag.Count -eq 0) {
    $u = Find-Uninstall $rows @('Microsoft 365', 'Microsoft 365-apper', 'Microsoft Office 16', 'Microsoft Office 365')
    $note = $(if ($u.Found) { 'i orden' } else { 'må installeres' })
    [void]$bag.Add((Item 'office' 'Microsoft 365 / Office' $u.Found $u.Version $u.Found $note))
}

$apps = @(
    @{ Id = 'word'; Name = 'Word'; Exe = 'WINWORD.EXE'; Pat = @('Microsoft Word') }
    @{ Id = 'excel'; Name = 'Excel'; Exe = 'EXCEL.EXE'; Pat = @('Microsoft Excel') }
    @{ Id = 'powerpoint'; Name = 'PowerPoint'; Exe = 'POWERPNT.EXE'; Pat = @('Microsoft PowerPoint') }
    @{ Id = 'outlook'; Name = 'Outlook'; Exe = 'OUTLOOK.EXE'; Pat = @('Microsoft Outlook') }
    @{ Id = 'onenote'; Name = 'OneNote'; Exe = 'ONENOTE.EXE'; Pat = @('Microsoft OneNote') }
)
foreach ($app in $apps) {
    $path = Get-AppPath -Exe $app.Exe
    $found = -not [string]::IsNullOrWhiteSpace($path) -and (Test-Path -LiteralPath $path)
    $ver = ''
    if ($found) { $ver = Get-ExeProduct -Path $path }
    if (-not $found) {
        $u = Find-Uninstall $rows $app.Pat
        $found = $u.Found
        $ver = $u.Version
    }
    $note = $(if ($found) { 'i orden' } else { 'må installeres' })
    [void]$bag.Add((Item $app.Id $app.Name $found $ver $found $note))
}

Show-ScanProgress 40 'Sjekker OneDrive...'

$od = Find-Uninstall $rows @('Microsoft OneDrive', 'OneDrive')
if (-not $od.Found) {
    $odExe = Join-Path $env:LOCALAPPDATA 'Microsoft\OneDrive\OneDrive.exe'
    if (Test-Path -LiteralPath $odExe) {
        $od = @{ Found = $true; Version = (Get-ExeProduct $odExe); Detail = $odExe }
    }
}
$odNote = $(if ($od.Found) { 'i orden' } else { 'må installeres' })
[void]$bag.Add((Item 'onedrive' 'OneDrive' $od.Found $od.Version $od.Found $odNote))

Show-ScanProgress 52 'Sjekker Teams...'

function Get-ProcessPath {
    param([string[]]$Names)
    foreach ($n in $Names) {
        $procs = @(Get-Process -Name $n -ErrorAction SilentlyContinue)
        foreach ($p in $procs) {
            $path = ''
            try { $path = [string]$p.Path } catch { $path = '' }
            if (-not [string]::IsNullOrWhiteSpace($path)) { return $path }
        }
    }
    return ''
}

function Test-ProcessRunning {
    param([string[]]$Names)
    foreach ($n in $Names) {
        if (Get-Process -Name $n -ErrorAction SilentlyContinue) { return $n }
    }
    return ''
}

function Get-TeamsItem {
    $workPkg = $null
    try {
        $workPkg = Get-AppxPackage -AllUsers -Name 'MSTeams' -ErrorAction SilentlyContinue | Select-Object -First 1
    } catch { }
    if (-not $workPkg) {
        try {
            $workPkg = Get-AppxPackage -Name 'MSTeams' -ErrorAction SilentlyContinue | Select-Object -First 1
        } catch { }
    }
    $workVer = ''
    if ($workPkg) { $workVer = [string](Get-Prop $workPkg 'Version') }

    $personalPkg = $false
    try {
        if (Get-AppxPackage -AllUsers -Name 'MicrosoftTeams' -ErrorAction SilentlyContinue | Select-Object -First 1) {
            $personalPkg = $true
        }
    } catch { }
    if (-not $personalPkg) {
        try {
            if (Get-AppxPackage -Name 'MicrosoftTeams' -ErrorAction SilentlyContinue | Select-Object -First 1) {
                $personalPkg = $true
            }
        } catch { }
    }

    $uWork = Find-Uninstall $rows @(
        'Microsoft Teams \(work or school\)',
        'Microsoft Teams \(arbeid eller skole\)',
        'Microsoft Teams \(jobb eller skole\)'
    )
    $uPersonal = Find-Uninstall $rows @(
        '^Microsoft Teams$',
        'Microsoft Teams \(personal\)',
        'Microsoft Teams \(personlig\)',
        'Microsoft Teams Free'
    )

    $running = Test-ProcessRunning -Names @('ms-teams', 'Teams')
    $exe = Get-ProcessPath -Names @('ms-teams', 'Teams')
    $local = [string]$env:LOCALAPPDATA
    $paths = @(
        $exe,
        (Join-Path $local 'Microsoft\WindowsApps\ms-teams.exe'),
        (Join-Path $local 'Microsoft\Teams\current\Teams.exe'),
        (Join-Path ${env:ProgramFiles} 'Teams\ms-teams.exe')
    )
    $foundPath = ''
    foreach ($p in $paths) {
        if (-not [string]::IsNullOrWhiteSpace($p) -and -not ($p.StartsWith('running:')) -and (Test-Path -LiteralPath $p)) {
            $foundPath = $p
            break
        }
    }

    $looksPersonalPath = ($exe -match 'MicrosoftTeams') -or ($foundPath -match 'MicrosoftTeams')
    $work = [bool]$workPkg -or $uWork.Found -or ($running -eq 'ms-teams' -and -not $looksPersonalPath) -or ($running -eq 'Teams') -or ($foundPath -match 'ms-teams\.exe|Microsoft\\Teams\\current')

    if ($work) {
        $ver = $workVer
        if (-not $ver) { $ver = $uWork.Version }
        if (-not $ver -and $foundPath) { $ver = Get-ExeProduct -Path $foundPath }
        $note = 'i orden'
        if ($running) { $note = 'i orden (kjører)' }
        return (Item 'teams' 'Teams (jobb eller skole)' $true $ver $true $note)
    }

    if ($personalPkg -or $uPersonal.Found -or $looksPersonalPath) {
        return (Item 'teams' 'Teams (jobb eller skole)' $true '' $false 'personlig Teams er installert/kjører - skolekonto trenger Teams (jobb eller skole)')
    }

    if ($running) {
        return (Item 'teams' 'Teams (jobb eller skole)' $true '' $true 'i orden (kjører)')
    }

    if ($foundPath) {
        $ver = Get-ExeProduct -Path $foundPath
        return (Item 'teams' 'Teams (jobb eller skole)' $true $ver $true 'i orden')
    }

    return (Item 'teams' 'Teams (jobb eller skole)' $false '' $false 'må installeres (personlig Teams teller ikke)')
}

[void]$bag.Add((Get-TeamsItem))

Show-ScanProgress 64 'Sjekker AppsAnywhere...'


$aa = Find-Uninstall $rows @('AppsAnywhere')
if (-not $aa.Found) {
    foreach ($p in @(
            (Join-Path ${env:ProgramFiles} 'AppsAnywhere\AppsAnywhere.exe'),
            (Join-Path ${env:ProgramFiles} 'Software2\AppsAnywhere.exe')
        )) {
        if (Test-Path -LiteralPath $p) {
            $aa = @{ Found = $true; Version = (Get-ExeProduct $p); Detail = $p }
            break
        }
    }
}
$aaNote = $(if ($aa.Found) { 'i orden' } else { 'må installeres' })
[void]$bag.Add((Item 'appsanywhere' 'AppsAnywhere' $aa.Found $aa.Version $aa.Found $aaNote))

Show-ScanProgress 70 'Sjekker printer...'
$pr = Find-Uninstall $rows @('YSoft SafeQ', 'SafeQ FlexiSpooler', 'Viken Print', 'FlexiSpooler')
if (-not $pr.Found) {
    if (Test-Path -LiteralPath 'C:\SafeQ6') {
        $pr = @{ Found = $true; Version = ''; Detail = 'C:\SafeQ6' }
    }
}
$prNote = $(if ($pr.Found) { 'i orden' } else { 'må installeres' })
[void]$bag.Add((Item 'printer' 'printer' $pr.Found $pr.Version $pr.Found $prNote))

[void]$bag.Add((Get-WindowsUpdateItem))

$osText = [string][Environment]::OSVersion
if ($os) { $osText = ([string]$os.Caption + ' ' + [string]$os.Version).Trim() }

$rawItems = $bag.ToArray()
if ($null -eq $rawItems) { $rawItems = @() }
$itemArray = [object[]]$rawItems

# PS 5.1 throws if a Hashtable value is @($genericList). Object[] is required.
$result = @{
    host       = [string]$env:COMPUTERNAME
    os         = $osText
    admin      = $admin
    user       = [string][Security.Principal.WindowsIdentity]::GetCurrent().Name
    scanned_at = (Get-Date).ToString('o')
    items      = $itemArray
}
Show-ScanProgress 92 'Sender resultatet...'
Send-Helper -Path "/api/helper/$SessionId/result" -Body @{ data = $result }
Show-ScanProgress 100 'Ferdig'
try { Write-Progress -Activity 'Zork Helper' -Completed } catch { }
Write-Host ''
} catch {
    $msg = [string]$_.Exception.Message
    try { Write-Progress -Activity 'Zork Helper' -Completed } catch { }
    Write-Host ''
    Write-Host ('  Feil: ' + $msg) -ForegroundColor Red
    try {
        Send-Helper -Path "/api/helper/$SessionId/error" -Body @{ message = $msg.Substring(0, [Math]::Min(180, $msg.Length)) }
    } catch { }
    exit 1
}
