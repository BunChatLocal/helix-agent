---
name: frid-nettverk
description: Getting a pupil or staff device onto FRID wifi — the FRID-iot bootstrap network, the certificate from onboard.frid-iks.no, Windows ClearPass QuickConnect, account activation and password portals per county, Authenticator/MFA setup, and the school apps installer.
---

# FRID network and school accounts

Facts about how pupils actually get online and into their account. Wifi steps
come from <https://onboard.frid-iks.no/> and from the portal's written Windows
PDF (`frid_pdf_veiledning.pdf`, oppdatert 9/9-25). **Never improvise an
alternative to these steps** — a confident wrong answer sends the pupil to the
IT desk anyway, and wifi onboarding here does not work the way it does in most
organisations.

In particular: connecting is **not** a matter of typing the school account and
password into the wifi prompt. It needs a certificate, installed first. On
Windows that installer is **ClearPass QuickConnect** (`ArubaQuickConnect.exe`),
opened from **Start QuickConnect** after they click **Last ned**. It is not a
`.cer` file they double-click.

## Getting onto FRID wifi

The order matters, and step 1 catches almost everyone:

1. Connect to the bootstrap network **`FRID-iot`**, password **`friddeploy`**.
   This only exists at school. Nothing below works from home.
2. Download the certificate. After they are on **`FRID-iot`**, **Last ned**
   on <https://onboard.frid-iks.no/> (`gjesteportal.frid-iks.no`). The
   download **only works while on `FRID-iot`**. Do not send them to login
   before they have joined that network.
3. Install the certificate by following the guide for that device.
4. Connect to the wifi network named **`FRID`**. Then forget **`FRID-iot`**.
   Do not send them to **FRID-giest**.

**Maximum 2 devices per person.** Someone on their third device will fail after
doing everything right. Do not ask this up front. Ask how many they already
have connected only if the certificate installed and FRID still refuses them.

The yellow box on the portal still prints an old bootstrap password. Ignore it.
Always give **`friddeploy`**. `!skolestart!` is the old one — do not use it.

**This Windows PC, in Zorken:** do not walk the portal steps. Tell them to
click **FRID-nett** under the chat. That starts Zork Helper if it is not by
the clock. Helper then checks wifi: already on **FRID** is Done. On
**FRID-iot** it forgets that network and joins it again with the bootstrap
password. On any other network it joins **FRID-iot** if it is in range.
The login window (same page as Last ned) opens only after they are on
**FRID-iot**. After they sign in, Helper clicks **Start QuickConnect**, then
Next and Ja. They should not click Installer on the card. Phones,
Macs, iPhones and Android have no Zork Helper: use the old steps below.

On Windows they also need a working school email and Microsoft Authenticator
**before** QuickConnect will let them onto **FRID**. They can still join
**FRID-iot** without that. If Last ned or QuickConnect asks them to sign in
and they cannot, pause wifi and use the account-activation procedure.

### Per-device guides

- **Windows** — written steps below, taken from the portal PDF. There is also
  a video: <https://onboard.frid-iks.no/Frid_Wifi_Windows_v1.6_sound.mp4>
- **macOS / iOS / Android** — video only. There is no written click-path for
  these. Send them to the matching video. Do not construct steps.
  - macOS: <https://onboard.frid-iks.no/Frid_Wifi_Mac_Lyd.mp4>
  - iOS: <https://onboard.frid-iks.no/Frid_Wifi_IOS_Lyd.mp4>
  - Android: <https://onboard.frid-iks.no/Frid_Wifi_android_v1.5_sound.mp4>

If someone has a device outside those four, or the guide doesn't match what they
see, send them to the portal or to IT at their school. Do not construct steps
for a platform we have no guide for.

Some screenshots still say **Viken** (old county name) on a login page or on a
certificate warning. The buttons are the ones named below. The wifi names to
use are **FRID-iot** and **FRID**.

## How to walk someone through this

Do not interview them. The first steps are the same on every device, and a
pupil who has to answer a checklist before they get something to do will leave.

Give the next action they can take. A question is not an action. Never ask
them to open the network list, name the networks they see, or say which
device they have — that is diagnosing, and it loses them.

Walk long procedures in batches. One or two images per reply, placed under the
step they illustrate. Never write that they *are* connected, or that anything
*is now* done — tell them the next click.

1. **School or home** — ask this only if they have not already said. Nothing
   onboarding-related works from home. If they are at home, say so and stop.
2. **Once they are at school, give the next click, then wait.** Do not add
   a question.

   - If this browser is **Windows**: if they can see **FRID-nett** under
     the chat, tell them to press **FRID-nett**. One instruction. They
     approve in Windows or the browser if asked. Do not also send them to
     **Last ned** or the portal first. If the button is not there, they
     are already on **FRID** — say so and stop. Do not send them through
     login.
   - Otherwise (phone, Mac, iPad, Android), say this in Norwegian bokmål, then wait:

   Greit. Koble deg først til wifi-nettverket **FRID-iot** med passordet
   **friddeploy**. Når du er på det nettverket, gå til
   <https://onboard.frid-iks.no/> og trykk **Last ned**. Logg inn med
   skolekontoen.

   Those two beats are one instruction, not two turns. The password is
   `friddeploy`. `!skolestart!` is the old one — do not use it. Do not open
   login before they are on **FRID-iot**. There is no certificate button
   under this chat on phones or Macs. Device videos stay on the portal if
   they need them after they have signed in.

   Show the join dialog with the first instruction when it helps:

   ![Koble til FRID-iot – skriv passordet og klikk Neste](asset:wifi-frid-iot-passord-20260910.png)

   ![Sertifikat-kortet – klikk Last ned](asset:wifi-last-ned-20260910.png)
3. **Which device** — if this turn already says they are on Windows, macOS,
   iOS or Android, use that guide. Do not ask. Ask **only** after they say
   they have clicked download (or FRID-nett) **and** you do not already know
   the device.
4. **How many devices already connected** — only if they installed the
   certificate and FRID still refuses them.
5. **Account activation / Authenticator** — only if that is what they asked
   about, or they cannot sign in when Last ned / QuickConnect asks them to.

## Windows — ClearPass QuickConnect

**Gjelder:** Windows-PC (veiledningen er tatt fra PDF-en for Windows).

Send stegene i bolker. Steg 1 er første beskjed over. Når de har trykket
**Last ned**, send Steg 2, vent, send Steg 3, vent, osv. Spør «Er du klar for
neste steg?» mellom bolkene.

**Steg 2 – Start QuickConnect**
1. Etter **Last ned sertifikat** (eller **Last ned** on the portal) åpnes en
   side på `gjesteportal.frid-iks.no`. Klikk **Start QuickConnect**.
   ![Start QuickConnect – last ned konfigurasjonsappen](asset:wifi-start-quickconnect-20260910.png)
2. Filen **ArubaQuickConnect.exe** lastes ned. I Edge: klikk **Åpne fil**.
   ![Nedlastinger – ArubaQuickConnect.exe – klikk Åpne fil](asset:wifi-apne-fil-20260910.png)
3. Om de blir bedt om å logge på: bruk skole-e-post
   (`bruker@afk.no` for en elev her) og passord, deretter **Neste**.
   Skjermen kan fortsatt si Viken. Får de ikke logget på, stopp wifi her og
   gå til kontoaktivering.
   ![Logg på med skole-e-post – klikk Neste](asset:wifi-logg-pa-20260910.png)

**Steg 3 – Next og Ja**
4. Programmet **ClearPass QuickConnect** / **FRID onboard** åpner. Klikk
   **Next**.
   ![FRID onboard – klikk Next](asset:wifi-quickconnect-next-20260910.png)
5. Windows viser flere advarsler. Klikk **Ja** på hver av dem (sertifikat,
   og eventuelt posisjon). Teksten kan si **Viken Root CA** — det er
   likevel **Ja**.
   ![Sikkerhetsadvarsel – klikk Ja for å installere sertifikatet](asset:wifi-sertifikat-ja-20260910.png)
   ![Posisjonstilgang – klikk Ja](asset:wifi-posisjon-ja-20260910.png)

**Steg 4 – Finish eller Connect**
6. Når konfigureringen er ferdig, er det to varianter. Følg den de ser:
   - **Finish** — klikk **Finish**. Koble deretter til **FRID** manuelt:
     wifi-listen → **FRID** → huk av **Koble til automatisk** → **Koble til**.
     ![FRID onboard – klikk Finish](asset:wifi-finish-20260910.png)
     ![Wifi-listen – FRID – klikk Koble til](asset:wifi-koble-til-frid-20260910.png)
   - **Connect** — klikk **Connect**, deretter **Close**.
     ![FRID onboard – klikk Connect](asset:wifi-connect-20260910.png)

**Steg 5 – Fjern FRID-iot**
7. Sjekk at de er på **FRID**. Hvis ikke, koble til **FRID**. Deretter fjern
   **FRID-iot**: klikk wifi-symbolet på oppgavelinja, klikk pilen til høyre
   for wifi-symbolet, høyreklikk **FRID-iot**, klikk **Fjern**.

Når de har fjernet **FRID-iot** og står på **FRID**, er Windows-PCen på
skolenettet. Si det slik, knyttet til det de nettopp gjorde — ikke som om
det allerede har skjedd av seg selv.

**Verifisert:** 2026-09-10 (portal + `frid_pdf_veiledning.pdf`)

## macOS, iPhone/iPad, Android

After they have clicked **Last ned**, send the matching video and wait. Do
not invent Settings paths, profile names or certificate menus.

Si for eksempel: Åpne videoen for enheten din og gjør det den viser. Si ifra
når du står fast, eller hva du ser på skjermen.

The Last ned click on these devices is on
<https://onboard.frid-iks.no/>, which opens
<https://gjesteportal.frid-iks.no/onboard/frid_provisioning.php> — only while
they are on **FRID-iot**. There is no **Last ned sertifikat** button in this
chat on those devices.

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
  portal has QR codes for both
  (<https://apps.apple.com/us/app/microsoft-authenticator/id983156458> and
  <https://play.google.com/store/apps/details?id=com.azure.authenticator>).
- Video: <https://onboard.frid-iks.no/MicrosoftAuth_Veiledning_sound.mp4>

A pupil replacing a phone will usually need MFA re-registered, and depending on
what they still have access to, that may need help from IT at their school.

## School apps (Windows only)

`FylkesApper.exe` from the portal (**Last ned .exe**, under Installer
skoleapper) installs the necessary school apps on a Windows 11 PC: **Office 365,
printer, AppsAnywhere, Ordnett, Teams**.

Only works on the school network, and only on Windows 11. There is no
equivalent for Mac, iOS or Android — don't offer one. Do this after they are
on **FRID**, not while they are still only on **FRID-iot**.

## Escalate when

- The account is locked, or the county portal refuses to activate or reset it
- They are already at the 2-device limit and genuinely need a third
- MFA needs re-registering and they can't get in to do it themselves
- The certificate installs but the FRID network still refuses the device
- Anything requiring admin rights on the device — a pupil has none
- Last ned does nothing, and they are not on **FRID-iot** at school

## Verified local knowledge

_(Add confirmed findings here as you resolve tickets. Keep it structural — no
usernames, machine names tied to people, or case details.)_
