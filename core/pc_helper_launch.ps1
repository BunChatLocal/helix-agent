$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$base = '__BASE_URL__'
$id = '__SESSION_ID__'
$secret = '__SECRET__'
$jobKind = '__JOB_KIND__'
$jobItems = '__JOB_ITEMS__'
$boot = Join-Path $env:TEMP ('zork-boot-' + $id + '.ps1')
$wc = New-Object System.Net.WebClient
$wc.Encoding = [Text.Encoding]::UTF8
$wc.Headers.Add('X-Helper-Secret', $secret)
try {
  $wc.DownloadFile($base + '/api/helper/' + $id + '/bootstrap', $boot)
  $bootArgs = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $boot,
    '-BaseUrl', $base,
    '-SessionId', $id,
    '-Secret', $secret,
    '-JobKind', $jobKind
  )
  if (-not [string]::IsNullOrWhiteSpace($jobItems)) {
    $bootArgs += @('-JobItems', $jobItems)
  }
  & powershell.exe @bootArgs
  $code = $LASTEXITCODE
} catch {
  $msg = [string]$_.Exception.Message
  try {
    $wc.Headers['Content-Type'] = 'application/json; charset=utf-8'
    $body = (@{ message = $msg.Substring(0, [Math]::Min(180, $msg.Length)) } | ConvertTo-Json -Compress)
    [void]$wc.UploadString($base + '/api/helper/' + $id + '/error', 'POST', $body)
  } catch {}
  Write-Host $msg
  Write-Host 'Zork Helper failed. Press a key to close.'
  try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { Start-Sleep -Seconds 20 }
  exit 1
} finally {
  Remove-Item -LiteralPath $boot -Force -ErrorAction SilentlyContinue
}
if ($code -and $code -ne 0) { exit $code }
