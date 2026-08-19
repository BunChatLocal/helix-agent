# TOOLS.md — This Environment

Facts about the machine and tenant you run on. Keep this accurate — stale
entries here cause confident wrong answers.

## System

- **OS:** Windows 11 Enterprise (26xxx)
- **Shell:** Windows PowerShell 5.1 is the default. Note the limits: no `&&`/`||`
  chaining, no ternary, no `??`, no `-AsHashtable`. Use `;` and `if ($?)`.
- **Package manager:** winget (available)
- **Identity:** device is Entra-joined (`AzureAdJoined: YES`, `DomainJoined: NO`)
- **Tenant:** FRID IKS
- **Python:** only inside Helix's own venv. No system Python on PATH.
- **Node:** not installed

## Admin tooling — NOT installed

Do not suggest commands that need these unless you first say they must be
installed:

- Azure CLI (`az`) — not installed
- `Az` PowerShell module — not installed
- `Microsoft.Graph` PowerShell — not installed
- `ExchangeOnlineManagement` — not installed
- `PnP.PowerShell` — not installed

This matters: you cannot query the tenant. When a question needs live tenant
data, say what you'd need to look up and where in the admin centre to find it,
or give the command to run once the module is installed.

## Diagnostic commands to GIVE PEOPLE

You cannot run these. Hand them over when they'd help, one at a time, and
interpret what the person pastes back. For a non-technical person, prefer a
UI equivalent if one exists — most people find "open Settings and look at X"
far easier than a PowerShell command.

```powershell
dsregcmd /status                      # Entra join state, device ID, PRT
Get-ComputerInfo                      # OS build, install date, memory
Get-Service <name>                    # service state
Get-NetAdapter; ipconfig /all         # network
Test-NetConnection <host> -Port <n>   # connectivity
Get-WinEvent -LogName System -MaxEvents 50
winget list                           # installed packages
Get-Printer; Get-PrintJob             # printing
gpresult /r                           # applied policy (Entra: limited)
Get-MpComputerStatus                  # Defender
```

## Web admin UI

- Helix admin UI: http://127.0.0.1:18791
- Logs: `~/.helix/logs/helix.log`

## Notes

- You have no access to the Visma systems directly. You reason about them from
  `skills/visma-*/SKILL.md` and from what the user tells you they see.
- Add environment facts here as you learn them — printer server names, VPN
  endpoints, standard image versions. No personal data.
