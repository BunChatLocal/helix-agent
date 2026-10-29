# /diag — Hand over a health check

You cannot run anything. Produce a short diagnostic script for the person to run
themselves, then interpret what they paste back.

1. **Ask which machine** we're diagnosing, if it isn't already clear.

2. **Give them one copyable block** — not a list of separate commands. Keep it
   to what's relevant to their reported problem; don't dump everything.

```powershell
dsregcmd /status
Get-ComputerInfo | Select OsName,OsVersion,OsBuildNumber
Get-Volume | Where DriveLetter
Get-Service | Where {$_.StartType -eq 'Automatic' -and $_.Status -ne 'Running'}
Get-WinEvent -LogName System -MaxEvents 40 | Where LevelDisplayName -eq 'Error' | Select TimeCreated,Id,ProviderName
```

3. **Tell them how to run it** in one line — open Start, type "PowerShell",
   paste, press Enter, copy the output back. Assume they've never opened
   PowerShell before.

4. **When they paste the output, interpret it.** Call out specifically:
   - `AzureAdJoined` / `AzureAdPrt` not YES
   - disk under ~10% free
   - automatic services not running
   - repeated errors from the same provider

Report as: **looks fine** / **worth a look** / **problem**, with the next step.
Don't echo their output back at them — summarise what it means.
