---
name: entra-azure
description: Entra ID and Azure — sign-in failures, MFA, conditional access, licences, groups. Advisory only; no tenant access.
---

# Entra ID / Azure

**You cannot query the tenant.** No `az`, no `Az`, no `Microsoft.Graph` module
installed. You reason from symptoms and tell the user where to look in the admin
centre, or give them the command to run once tooling is installed.

## Scoping questions

- One user or many? Same licence type, same group, same location?
- Which client — Outlook desktop, browser, mobile, Teams?
- Exact error code shown? (Entra codes are `AADSTS<number>` — ask for it, don't guess)
- Did it start after a password change, a role change, or a new device?

## Sign-in error codes worth recognising

Ask for the `AADSTS` code; it narrows things fast. Common ones:

- `AADSTS50126` — invalid username or password
- `AADSTS50053` — account locked / smart lockout
- `AADSTS50055` — password expired
- `AADSTS50058` — silent sign-in failed, session missing
- `AADSTS50076` / `50079` — MFA required / MFA enrolment required
- `AADSTS53003` — blocked by conditional access
- `AADSTS700016` — application not found in tenant
- `AADSTS90072` — account exists in another tenant, not a guest here

If a code is not on this list, say you'd need to look it up rather than
guessing at its meaning.

## Where to look (admin centres)

- **Entra admin centre** → Users → Sign-in logs — per-user failures, CA policy applied
- **Entra** → Protection → Conditional Access — which policy matched
- **M365 admin centre** → Users → Licences — assignment and service plan state
- **Entra** → Groups — dynamic membership rules and processing state

## Licence and group patterns

Group-based licensing is the usual cause of "user lost access to X". Check
whether the user left a group, whether the group licence has enough seats, and
whether a dynamic rule stopped matching after an attribute change (department,
job title). Attribute changes often originate in the HR system — for this
organisation that means Visma Enterprise, so a licence problem can actually be a
Visma sync problem. See `skills/visma-enterprise/SKILL.md`.

## Commands (only once tooling is installed)

```powershell
Install-Module Microsoft.Graph -Scope CurrentUser   # not installed today
Connect-MgGraph -Scopes "User.Read.All","AuditLog.Read.All"
Get-MgUser -UserId <upn> | Select DisplayName,AccountEnabled,UserType
Get-MgUserLicenseDetail -UserId <upn>
Get-MgAuditLogSignIn -Filter "userPrincipalName eq '<upn>'" -Top 10
```

State clearly that these need installing and a sign-in first, and that they read
production data.

## Hard limits

Never propose that you perform: password resets, licence changes, group
membership changes, CA policy edits, or account enable/disable. Write the
command or click-path and hand it over.

## Verified local knowledge

_(Add confirmed tenant-specific patterns here — naming conventions, which groups
drive which licences, known CA policies. No personal data.)_
