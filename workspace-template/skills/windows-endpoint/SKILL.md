---
name: devices
description: Windows 11 endpoint troubleshooting — boot, performance, profiles, printing, drivers, Intune/Entra join state.
---

# Devices

## Scoping questions

- One device or several? One user or several on the same device?
- When did it start — after an update, a policy change, a password change?
- Is the device on the corporate network, VPN, or home?
- Does it reproduce for the same user on a different device? (separates
  profile/identity problems from device problems)

## Read-only diagnostics

```powershell
dsregcmd /status                          # AzureAdJoined, PRT state, tenant
Get-ComputerInfo | Select OsName,OsVersion,OsBuildNumber,CsName
Get-WinEvent -LogName System -MaxEvents 50 | Where LevelDisplayName -in 'Error','Warning'
Get-WinEvent -LogName Application -MaxEvents 50 | Where LevelDisplayName -eq 'Error'
Get-Service | Where Status -ne 'Running' | Where StartType -eq 'Automatic'
Get-Volume                                 # disk pressure
Get-Process | Sort CPU -Descending | Select -First 10
Test-NetConnection <host> -Port 443
Get-Printer; Get-PrintJob -PrinterName <name>
Get-MpComputerStatus | Select AntivirusEnabled,RealTimeProtectionEnabled
```

## Common patterns

**PRT missing / repeated sign-in prompts**
`dsregcmd /status` → `AzureAdPrt : NO`. Usually resolves after a lock/unlock or
reboot on a network that can reach Entra. If persistent, the device may need
re-registration — that is a change, so escalate rather than perform.

**Slow logon**
Check profile size, roaming/known-folder redirect state, and whether it's only
first logon after an update. Compare with the same user on another device.

**Printing**
Separate the queue from the driver from the connection. `Get-PrintJob` shows
stuck jobs; a job stuck across users points at the server or driver, a single
user's stuck job points at the client.

**Post-update regressions**
`Get-ComputerInfo` for build number, correlate with the update ring. If several
devices on the same build broke together, it is a deployment issue — escalate,
don't chase it per-device.

## Escalate when

- The fix requires re-joining, reimaging, or driver installation
- Multiple devices are affected — likely policy, ring, or infrastructure
- The problem is in Intune policy assignment (you cannot read the tenant)

## Verified local knowledge

_(Add confirmed findings here as you resolve tickets. Keep it structural — no
usernames, machine names tied to people, or case details.)_
