---
name: m365-office
description: Microsoft 365 — Outlook, Teams, OneDrive, SharePoint, Excel/Word. Client-side troubleshooting and where to look in admin centres.
---

# Microsoft 365 / Office

## Scoping questions

- Which client — desktop, web (OWA/Teams web), or mobile? Does the web client work?
  (web working but desktop failing isolates it to the client or its cached identity)
- One user or several? Same site/mailbox or different?
- Exact error text or code — ask, don't guess.

## Outlook

**Repeated credential prompts** — usually a stale cached identity or a broken
PRT. Check `dsregcmd /status` first. The Office identity cache lives under
`%LOCALAPPDATA%\Microsoft\Office\16.0\Licensing` and the account list in
`HKCU\Software\Microsoft\Office\16.0\Common\Identity`. Reading is fine; clearing
those is a change — hand it over.

**Mail not sending/receiving but OWA works** — client-side. Check add-ins, cached
mode, OST size, and whether the profile is over quota.

**Shared mailbox missing** — permission propagation can take time; verify the
permission actually exists in Exchange admin centre before troubleshooting the
client.

## Teams

Cache issues are the most common cause of odd UI behaviour. The cache path for
the new Teams client is under
`%LOCALAPPDATA%\Packages\MSTeams_8wekyb3d8bbwe\LocalCache`. Clearing it is a
change — give the path and let the user do it.

Missing team or channel is usually group membership, not Teams. Check the
underlying M365 group.

## OneDrive / SharePoint

- Sync problems: check available disk, path length (260-char limit still bites
  with deep SharePoint libraries), and file name characters
- "Processing changes" forever — usually a single large or locked file
- Known Folder Move failures — check policy assignment, then local disk space

Read-only checks:

```powershell
Get-Process OneDrive | Select StartTime,CPU
Get-Volume
(Get-Item "$env:LOCALAPPDATA\Microsoft\OneDrive\logs") | Get-ChildItem | Select -Last 5
```

## Excel / Word

Slow or crashing: test with add-ins disabled (safe mode `excel /safe`), check
whether the file is on a synced OneDrive path, and whether it's a specific file
or all files. A single slow file is usually volatile formulas or external links,
not the install.

## Where to look

- **M365 admin centre** → Health → Service health — rule out a live incident first
- **Exchange admin centre** → Recipients — mailbox permissions, forwarding
- **SharePoint admin centre** → Sites — storage, sharing settings

Always check service health before deep-diving a multi-user problem.

## Verified local knowledge

_(Add confirmed findings — standard site structures, known add-ins, org-specific
Teams policies. No personal data.)_
