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

_Fill this in yourself as you resolve tickets. Suggested format:_

```
### <Short problem title>
**Symptom:** what the user reports / sees
**Cause:** confirmed root cause
**Fix:** exact steps or click-path
**Verified:** YYYY-MM-DD
```

_No employee names, birth numbers, or case details — describe roles and record
types instead._

## Integration notes

_(Record here: what syncs from Visma Enterprise to Entra/AD, how often, which
attributes, and who owns the job. This is the highest-value thing to document —
it explains a large share of downstream account problems.)_
