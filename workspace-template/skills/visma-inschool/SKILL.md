---
name: visma-inschool
description: Visma InSchool (VIS) school administration. STRUCTURE ONLY — do not answer from general knowledge; use verified entries below or ask.
---

# Visma InSchool (VIS)

## Read this first

**Your general knowledge of this product is unreliable.** Visma InSchool is a
Norwegian school administration system, rolled out per county/municipality with
local configuration. Roles, permissions, timetable setup, and integrations vary.
Do not reconstruct screens from memory.

**Rules for this domain:**

1. Answer only from the **Verified procedures** section below, or from what the
   user describes on screen.
2. If it isn't documented here, say so and ask what they see.
3. Never state a menu path, role name, field, or error code you haven't read
   here or been told.
4. Timetable, assessment, and grade data are sensitive — see the personal-data
   rule in `AGENTS.md`. Never write pupil details into any file.

## What you can help with regardless

- **Role vs data.** Most "I can't see X" reports in school systems are role or
  permission scope (which school, which class, which period), not a bug. Ask
  which role the user holds and whether a colleague in the same role sees it.
- **Account provenance.** VIS accounts typically originate from an upstream
  identity source. If someone cannot log in at all, check whether it's a VIS
  problem or an Entra/identity problem first — that's usually faster to rule out.
  See `skills/entra-azure/SKILL.md`.
- **School year boundaries.** Many school-system oddities are period-related:
  a new school year, a class not yet activated, a pupil not yet enrolled, a
  teacher not yet assigned. Establish *which* school year and period the user is
  looking at before troubleshooting.
- **Timing.** Integrations and batch jobs run on schedules. Record them below.

## Escalate when

- It affects assessment, grades, or documentation with legal weight — do not
  speculate, route it to the system owner
- The behaviour is consistent across users in the same role
- It involves data exchange with national services

## Verified procedures

Write new entries as a `###` heading, then who it applies to, the steps, and
the date you confirmed it. Do not leave a blank heading in this section — an
unfilled template looks like a procedure. Never record pupil or staff names,
birth numbers, classes tied to individuals, or case details.

## Integration notes

_(Record here: which identity source feeds VIS, sync frequency, who owns it, and
what a sync failure looks like from the user's side.)_
