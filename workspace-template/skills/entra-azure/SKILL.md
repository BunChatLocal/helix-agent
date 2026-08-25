---
name: entra-azure
description: Entra ID and Azure — sign-in failures, MFA, conditional access, licences, groups. Advisory only; no tenant access.
---

# Entra ID / Azure

**You cannot query the tenant.** No `az`, no `Az`, no `Microsoft.Graph` module
installed. You reason from symptoms and tell the user where to look in the admin
centre, or give them the command to run once tooling is installed.

## Scoping questions

Skip this section when they asked how to do a procedure that is already
verified below (MFA reset, Bitlocker lookup). Give those steps. These
questions are for undiagnosed sign-in failures, not for "noen trenger
MFA-reset".

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

### Om Azure i Akershus fylkeskommune

**Hvem bruker det:** Kun ansatte i FRID (IT-avdelingen). Krever en egen
administratorkonto med prefiks **cadm** (på formen `cadm.<brukernavn>@afk.no`).
Vanlige brukerkontoer (`<brukernavn>@afk.no`) har ikke tilgang.

**Hva Azure brukes til:**
- Slå opp opplysninger om elever og ansatte
- Resette MFA (Microsoft Authenticator) for elever og ansatte når den ikke
  lenger fungerer
- Se hvilke telefoner og PC-er ansatte har registrert
- Se Bitlocker-nøkler

**Innlogging:** Gå til https://portal.azure.com og logg inn med cadm-kontoen.

## Verified procedures

### Resette MFA for en bruker

> **Visningsregel:** Når noen spør om denne prosedyren, vis ALLTID bildene sammen
> med stegene — ikke bare teksten.

**Gjelder:** FRID-ansatte med cadm-konto

**Del 1 — Aktiver rettigheten i Privileged Identity Management**
1. Søk etter «Microsoft Entra Privileged Identity Management» i søkefeltet
   øverst i Azure og klikk på det under Services i søkeresultatene.
   (Azure husker valget og legger det fremst på hjemmesiden til neste gang.)
   ![Søk etter PIM i Azure-søkefeltet](asset:dette-er-bildene-du-skal-sende-sammen-me-20260814-1.png)
2. Klikk **«My roles»** i venstremenyen under Tasks.
   ![My roles i venstremenyen under Tasks](asset:dette-er-bildene-du-skal-sende-sammen-me-20260814-2.png)
3. Finn rollen **«Authentication Administrator»** i listen og klikk **«Activate»**.
   ![Authentication Administrator med Activate-knapp](asset:dette-er-bildene-du-skal-sende-sammen-me-20260814-3.png)
4. I aktiveringsvinduet: skriv noe i feltet **«Reason (max 500 characters)»**
   — feltet kan ikke være tomt, men én enkelt bokstav er nok.
5. Klikk **«Activate»**. Rettigheten er nå aktivert.
   ![Reason-felt utfylt og Activate-knapp](asset:dette-er-bildene-du-skal-sende-sammen-me-20260814-4.png)

**Del 2 — Reset MFA**

Ikke krev brukernavn eller e-post før du gir stegene. Det eneste du trenger
fra den som spør, er **navnet** på personen. Azure-søket under er på navn.

6. Søk opp brukeren i søkefeltet øverst i Azure **på navn**. E-postadresse
   er ikke nødvendig.
7. Klikk på riktig bruker under «Microsoft Entra ID» i søkeresultatene.
   ![Søk opp bruker og klikk på riktig treff](asset:del-2-reset-mfa-20260814-1.png)
8. Klikk **«Authentication methods»** i venstremenyen.
   ![Authentication methods i venstremenyen](asset:del-2-reset-mfa-20260814-2.png)
9. Klikk **«Require re-register multifactor authentication»** i verktøylinjen øverst.
   ![Require re-register multifactor authentication](asset:del-2-reset-mfa-20260814-3.png)

**Merk:**
- For å se Bitlocker-nøkler aktiveres rollen **«Bitlocker Key Reader»** på
  samme måte i steg 3, i stedet for Authentication Administrator.
- Rettighetene må aktiveres på nytt **hver dag** — de er ikke permanent påslått.
- Azure skal kun brukes av FRID-ansatte.

**Verifisert:** 2026-08-14

## Integration notes

- **Privileged Identity Management — daglig aktivering:** Rettigheter som
  «Authentication Administrator» og «Bitlocker Key Reader» utløper daglig og
  må aktiveres på nytt hver dag før bruk via
  Microsoft Entra Privileged Identity Management → My roles → Activate.
