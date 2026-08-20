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

### Logge inn i Visma InSchool (Akershus)
**Gjelder:** Elever og foresatte ved offentlige videregående skoler i Akershus

**Slik gjør du:**
1. Gå til https://afk.no/tjenester/skole-og-opplaring/opplaring-i-skole/inschool/
2. Klikk på nedtrekksmenyen **«Finn din skole»** under «Logg inn i InSchool».
3. Velg skolen din i listen.
4. Du blir sendt direkte til innloggingssiden for den skolen.
5. Velg riktig innloggingsmetode:
   - **Elever** → **«Logg inn med Feide»** (kun dette alternativet)
   - **Foresatte** → **«Logg inn med ID-porten»** (for å lese beskjeder fra skolen)
6. På Feide-siden: sjekk at **«Your affiliation» / «Din tilhørighet»** viser
   **Akershus fylkeskommune**. Hvis ikke, klikk **«Change affiliation»** /
   **«Endre tilhørighet»** og velg riktig. Klikk deretter
   **«Use work or school account»** / **«Bruk arbeids- eller skolekonto»**.
7. På Akershus Fylkeskommune sin påloggingsside: skriv inn skole-e-posten din
   (på formen **bruker@afk.no**) i feltet **«E-post, telefon eller Skype»**,
   klikk **«Neste»**, og skriv inn passordet ditt.

**Merk:**
- Feide-siden kan vises på norsk eller engelsk avhengig av nettleserinnstillinger
  — knappetekst og feltnavn er det samme, bare oversatt.
- Hvis feil fylkeskommune vises under tilhørighet, vil innloggingen feile eller
  lande i feil organisasjon. Alltid sjekk at det står **Akershus fylkeskommune**
  før du klikker videre.
- Vet du ikke hva skole-e-posten din er, eller har glemt passordet? Ta kontakt
  med IT-avdelingen — de kan opplyse om e-postadressen og hjelpe med nytt passord.
- Hver skole har sin egen innloggingsside — du må velge riktig skole i listen,
  ellers havner du feil sted.
- Elever skal aldri bruke ID-porten — det vil ikke fungere for skoleoppgaver,
  karakterer eller timeplan.
- Listen inneholder **kun offentlige videregående skoler i Akershus**.
  - Private videregående skoler: sjekk skolens egen hjemmeside.
  - Ungdomsskoler og barneskoler bruker Visma Flyt Skole — ta kontakt med
    din kommune.
  - Videregående skoler utenfor Akershus: ta kontakt med din fylkeskommune.

**Verifisert:** 2026-08-13

### Aktivere ny brukerkonto (brukerkonto.afk.no)
**Gjelder:** Elever og ansatte ved videregående skoler i Akershus som skal
aktivere brukerkonto for første gang

**Slik veileder du brukeren gjennom denne prosedyren:**
Send stegene i bolker — ikke alt på én gang. Send Steg 1–3, vent på bekreftelse,
send Steg 4–5, vent på bekreftelse, osv. Spør alltid «Er du klar for neste steg?»
mellom hver bolke. Dette er en lang prosess og brukeren trenger tid på hvert steg.

**Du trenger før du starter:**
- MinID, BankID, Buypass eller Commfides
- En telefon med **Microsoft Authenticator** installert (App Store / Google Play)
- En oppdatert nettleser (Edge, Chrome, Safari eller Firefox)

**Slik gjør du:**

**Steg 1 – Logg inn med ID-porten**
1. Gå til https://brukerkonto.afk.no/
2. Klikk **«Logg inn med ID-porten»**.
   ![Logg inn via ID-porten – velg Aktiver ny konto](asset:kontoaktivering-idporten-start-20260814.png)
3. Velg innloggingsmetode: BankID, MinID, Buypass eller Commfides.
   ![Velg innloggingsmetode – MinID, BankID, Buypass eller Commfides](asset:kontoaktivering-idporten-metode-20260814.png)
4. Følg instruksjonene fra tilbyderen.

**Steg 2 – Systemet finner kontoen din**
5. Etter innlogging finner systemet brukerkontoen din automatisk.
   Dialogen **«Klar til å aktivere kontoen din»** vises — klikk **«Fortsett»**.
   ![Klar til å aktivere kontoen din – klikk Fortsett](asset:kontoaktivering-klar-til-aktivering-20260814.png)

**Steg 3 – Midlertidig tilgangspass (TAP) opprettes**
6. Dialogen **«Dine innloggingsdetaljer er klare»** vises med brukernavnet
   ditt og et midlertidig tilgangspass.
   - Noter brukernavnet.
   - Klikk **«Kopier»** for å kopiere tilgangspasset til utklippstavlen.
   - Tilgangspasset er gyldig i **60 minutter**.
7. Klikk **«Fortsett til arbeidskonto»**.
   ![Dine innloggingsdetaljer er klare – TAP-dialog med Kopier-knapp og Fortsett til arbeidskonto](asset:kontoaktivering-tap-dialog-20260814.png)
   ⚠️ **Kjent feil i teksten:** Dialogen sier «kan bare brukes én gang» —
   dette stemmer ikke. Koden er gyldig i 60 minutter og kan brukes flere
   ganger i løpet av aktiveringen.

**Steg 4 – Lim inn tilgangspasset**
8. Du videresendes automatisk til **login.microsoftonline.com**
   (Microsofts offisielle innloggingsside — trygt).
9. Lim inn tilgangspasset fra utklippstavlen.
   - **iPhone/iPad:** iOS kan vise en ny innloggingsprompt fra Microsoft.
     Skriv da inn brukernavnet fra Steg 3 og lim inn tilgangspasset på nytt.

**Steg 5 – Registrer 2-faktor (Microsoft Authenticator)**
10. Microsoft ber deg sette opp 2-faktorautentisering. Klikk **«Neste»**.
11. Åpne **Microsoft Authenticator** på telefonen og trykk QR-ikonet
    nederst til høyre.
12. Skann QR-koden som vises på skjermen.
13. Godkjenn varselet som dukker opp på telefonen.
14. Klikk **«Ferdig»** i nettleseren.
    ⚠️ 2-faktor er obligatorisk — uten fullført registrering får du ikke
    tilgang til kontoen.

**Steg 6 – Konto aktivert**
15. Skjermen **«Hei — Kontoen din er nå klar til bruk»** vises med et grønt
    hakemerke og teksten «Videresender om 10 sekunder...» samt knappen **«Hopp over»**.
    ![Kontoen din er nå klar til bruk – ikke ferdig ennå](asset:kontoaktivering-konto-klar-20260814.png)
    ⛔ **IKKE FERDIG.** Selv om skjermen ser ut som en avslutning, er du ikke
    ferdig. Du må fortsatt registrere tilgangsnøkkel (Steg 7) og endre passord
    (Steg 8). Vent på at siden videresender automatisk, eller klikk **«Hopp over»**
    for å gå videre med én gang.

**Steg 7 – Registrer tilgangsnøkkel**
16. Du videresendes automatisk til **aka.ms/mysecurityinfo**.
    - Har tilgangspasset utløpt (mer enn 60 min siden Steg 3): logg inn
      med brukernavn og Microsoft Authenticator i stedet.
17. Klikk **«+ Legg til en påloggingsmetode»**.
    ![Sikkerhetsinformasjon-siden – klikk Legg til en påloggingsmetode](asset:tilgangsnokkel-legg-til-metode-20260814.png)
18. Velg **«Tilgangsnøkkel»** fra listen og klikk **«Neste»**.
    ![Dialog: Legg til en påloggingsmetode – velg Tilgangsnøkkel](asset:tilgangsnokkel-velg-tilgangsnokkel-20260814.png)
19. Velg **«Microsoft Authenticator»** som enhet og skann QR-koden
    med telefonen.
20. Windows Security-dialogen **«Lagre tilgangsnøkkelen»** vises.
    Klikk **«Endring»** — ikke «Fortsett».
    ![Windows Security: Lagre tilgangsnøkkelen – klikk Endring](asset:tilgangsnokkel-windows-security-20260814.png)
21. Velg **«iPhone, iPad eller Android-enhet»** fra listen.
    Skann deretter QR-koden som vises med Microsoft Authenticator på telefonen.
    ![Velg lagringssted og skann QR-kode for å lagre tilgangsnøkkel på telefon](asset:tilgangsnokkel-velg-enhet-qr-20260814.png)
22. Bekreft i Authenticator-appen (fingeravtrykk, ansiktsgjenkjenning eller PIN).
23. Gi tilgangsnøkkelen et gjenkjennelig navn, f.eks. «min grønne iPhone»
    eller «tjenestetelefon».
24. Klikk **«Fullfør»** i nettleseren.

**Steg 8 – Endre passord**
25. På siden **Sikkerhetsinformasjon**, finn raden **«Passord»** og
    klikk **«Endre»**.
26. Klikk **«Har du glemt passordet?»**.
27. Skriv inn brukernavnet og koden som vises på bildet. Klikk **«Neste»**.
28. Bekreft identiteten via Microsoft Authenticator.
29. Skriv inn et nytt passord som oppfyller kravene (se under).
30. Ferdig — du kan nå bruke det nye passordet.

**Krav til passord:**
- Minimum 10 tegn
- Store og små bokstaver + minst ett tall
- Vanlige ord fra ordlister godkjennes ikke
- Deler av for- eller etternavn godkjennes ikke
- De 24 siste passordene kan ikke gjenbrukes
- 10 feil forsøk sperrer kontoen i 15 minutter

**Merk:**
- Installer Microsoft Authenticator på telefonen **før** du starter.
- Tilgangspasset fra Steg 3 er gyldig i **60 minutter** — ikke la det
  gå for lang tid mellom stegene.
- login.microsoftonline.com og aka.ms/mysecurityinfo er Microsofts
  offisielle sider og trygge å bruke selv om de ser fremmede ut.
- Dette er aktiveringsprosessen, ikke daglig innlogging — etter aktivering
  logger eleven inn med Feide (se «Logge inn i Visma InSchool (Akershus)»).

**Verifisert:** 2026-08-14

## Integration notes

_(Record here: which identity source feeds VIS, sync frequency, who owns it, and
what a sync failure looks like from the user's side.)_
