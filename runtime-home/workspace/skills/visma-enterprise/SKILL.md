---
name: visma-enterprise
description: Visma Enterprise (municipal HR/ERP). STRUCTURE ONLY — do not answer from general knowledge; use verified entries below or ask.
---

# Visma Enterprise

## Read this first

**Your general knowledge of this product is unreliable.** Visma Enterprise is a
Norwegian-market HR/ERP suite, deployed and configured per organisation. Module
selection, role names, screen layouts, approval chains and integrations differ
between customers. Anything you "remember" about its menus is likely wrong or
from a different version.

**Rules for this domain:**

1. Answer only from the **Verified procedures** section below, or from what the
   user tells you they see on screen.
2. If it isn't recorded here, say so: *"I don't have that documented — what do
   you see on that screen?"* Then walk them through it by asking.
3. Never state a menu path, field name, report name, or error code you have not
   read in this file or been told.
4. It is fine and expected to say "I don't know this one, but here's how I'd
   narrow it down."

## What you can help with regardless

Reasoning about the *shape* of a problem is still useful even without product
specifics:

- **Is it access or data?** Can the user log in at all? Can others in the same
  role do the thing? That separates permissions from a data/record problem.
- **Is it one record or the whole module?** One failing employee record behaves
  very differently from a broken module.
- **Did something upstream change?** HR/ERP is usually a *source* system. If
  someone's Entra account, licence, or group membership broke, check whether an
  HR change (new position, end date, changed department) flowed downstream.
  See `skills/entra-azure/SKILL.md`.
- **Timing** — many ERP integrations run on a schedule. "It'll appear tomorrow"
  is a real answer. Record the actual schedules below once known.
- **Period/lock state** — payroll and accounting periods get locked; an edit
  failing may be correct behaviour, not a fault.

## Escalate to the Visma vendor when

- The error appears server-side or references an integration/job failure
- The behaviour is consistent across users and roles
- It involves payroll calculation correctness — do not speculate about pay

## Verified procedures

Write new entries as a `###` heading, then symptom, cause, fix, and the date
you confirmed it. Do not leave a blank heading in this section — an unfilled
template looks like a procedure. No employee names, birth numbers, or case
details; describe roles and record types instead.

---

### Registrere egenmelding for en ansatt (lederoppgave)
**Gjelder:** Leder som registrerer egenmelding på vegne av en ansatt
**Slik gjør du:**
1. Gå til **Meny → Ansatt → Fravaer → Registrer fravaer**
2. Skriv inn den ansattes navn i feltet **Ansatt**
3. Kontroller at **Fravaerstype** er satt til **Egenmelding** — dette er standardverdi,
   men kan endres via nedtrekkslisten hvis en annen fraværstype er aktuelt
4. Fyll inn **Fra dato** og **Til dato**
5. Klikk **Lagre** — en grønn bekreftelsesmelding vises øverst på siden
**Merk:** Registreringen er ikke ferdig behandlet etter Lagre. Nærmeste leder
må i tillegg godkjenne fraværet under **Fravaer → Fravaersoversikt**.
**Verifisert:** 2026-08-14

## Integration notes

_(Record here: what syncs from Visma Enterprise to Entra/AD, how often, which
attributes, and who owns the job. This is the highest-value thing to document —
it explains a large share of downstream account problems.)_
