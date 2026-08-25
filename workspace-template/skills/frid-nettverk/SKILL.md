---
name: frid-nettverk
description: Getting a pupil or staff device onto FRID wifi — the FRID-iot bootstrap network, the certificate from onboard.frid-iks.no, account activation and password portals per county, Authenticator/MFA setup, and the school apps installer.
---

# FRID network and school accounts

Facts about how pupils actually get online and into their account. Everything
here comes from <https://onboard.frid-iks.no/>, which is the page we send people
to. **Never improvise an alternative to these steps** — a confident wrong answer
sends the pupil to the IT desk anyway, and wifi onboarding here does not work
the way it does in most organisations.

In particular: connecting is **not** a matter of typing the school account and
password into the wifi prompt. It needs a certificate, installed first.

## Getting onto FRID wifi

The order matters, and step 1 catches almost everyone:

1. Connect to the bootstrap network **`FRID-iot`**, password **`friddeploy`**.
   This only exists at school. Nothing below works from home.
2. Go to <https://onboard.frid-iks.no/> and download the certificate. The
   download **only works while on `FRID-iot`**.
3. Install the certificate by following the guide for that device.
4. Connect to the FRID wifi network.

**Maximum 2 devices per person.** Someone on their third device will fail after
doing everything right. Do not ask this up front. Ask how many they already
have connected only if the certificate installed and FRID still refuses them.

### Per-device guides

The portal has a video guide for each of **macOS, iOS, Android, Windows** under
"Videoveiledning for tilkobling til FRID trådløst nettverk". There is also a
written PDF guide, **Windows only**, at `frid_pdf_veiledning.pdf` on the portal.

If someone has a device outside those four, or the guide doesn't match what they
see, send them to the portal or to IT at their school. Do not construct steps
for a platform we have no guide for.

## How to walk someone through this

Do not interview them. The first steps are the same on every device, and a
pupil who has to answer a checklist before they get something to do will leave.

Give the next action they can take. A question is not an action. Never ask
them to open the network list, name the networks they see, or say which
device they have — that is diagnosing, and it loses them.

1. **School or home** — ask this only if they have not already said. Nothing
   onboarding-related works from home. If they are at home, say so and stop.
2. **Once they are at school, give this instruction in Norwegian bokmål,
   then wait.** Do not add a question. Say:

   Greit. Koble deg først til wifi-nettverket **FRID-iot** med passordet
   **friddeploy**. Når du er på det nettverket, gå til
   <https://onboard.frid-iks.no> og last ned sertifikatet.

   Those two beats are one instruction, not two turns. The password is
   `friddeploy`. `!skolestart!` is the old one — do not use it.
3. **Which device** — if this turn already says they are on Windows, macOS,
   iOS or Android, use that guide. Do not ask. Ask **only** after they say
   they have clicked download **and** you do not already know the device.
4. **How many devices already connected** — only if they installed the
   certificate and FRID still refuses them.
5. **Account activation / Authenticator** — only if that is what they asked
   about, or they cannot sign in to the portal.

## Accounts — activation, username, password

Each county has its own portal. All three do the same three things: **aktiver
konto**, **glemt brukernavn**, and **glemt passord / endre passord**.

| County   | Portal                                              |
| -------- | --------------------------------------------------- |
| Østfold  | <https://brukerkonto.ofk.no/>                       |
| Akershus | <https://brukerkonto.afk.no/>                       |
| Buskerud | <https://brukerkonto.bfk.no/>                       |

**A pupil talking to you is always Akershus** — only `@afk.no` accounts can sign
in here, so send them straight to <https://brukerkonto.afk.no/> without asking
which county they're at. The other two are listed for staff, who support all
three.

Self-service covers activation, a forgotten username and a password change. If
the portal itself refuses them, or the account is locked, that needs a person at
their school.

**First-time activation is a long procedure and it is written down — do not
improvise it.** The full step-by-step for `brukerkonto.afk.no`, including the
temporary access pass and its 60-minute limit, Authenticator enrolment, the
passkey step, the "looks finished but isn't" screen, and the password rules,
lives under **"Aktivere ny brukerkonto"** in the Visma InSchool pupil guide
(already included when you are talking to a pupil). Talking to a colleague:
the same steps are in `skills/visma-inschool/SKILL.md`. Walk them through it
in batches.

Signing in to Visma InSchool afterwards is a different thing, under
**"Logge inn i Visma InSchool (Akershus)"** — pupils use Feide, never ID-porten.

## Two-factor / Microsoft Authenticator

- Set up two-factor at <https://aka.ms/mfasetup>.
- The app is **Microsoft Authenticator**, from the App Store or Google Play. The
  portal has QR codes for both.
- The portal has a video guide for activating Authenticator.

A pupil replacing a phone will usually need MFA re-registered, and depending on
what they still have access to, that may need help from IT at their school.

## School apps (Windows only)

`FylkesApper.exe` from the portal installs the necessary school apps on a
Windows 11 PC: **Office 365, printer, AppsAnywhere, Ordnett, Teams**.

Only works on the school network, and only on Windows 11. There is no
equivalent for Mac, iOS or Android — don't offer one.

## Escalate when

- The account is locked, or the county portal refuses to activate or reset it
- They are already at the 2-device limit and genuinely need a third
- MFA needs re-registering and they can't get in to do it themselves
- The certificate installs but the FRID network still refuses the device
- Anything requiring admin rights on the device — a pupil has none

## Verified local knowledge

_(Add confirmed findings here as you resolve tickets. Keep it structural — no
usernames, machine names tied to people, or case details.)_
