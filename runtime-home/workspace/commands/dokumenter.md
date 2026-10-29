# /dokumenter — Turn screenshots into a permanent procedure

The user has walked through a system and captured screenshots. Your job is to
read them and write a procedure into the knowledge base, so the next person who
asks gets a direct answer instead of questions.

You are reading images. You cannot open the system yourself.

## 1. Read every image before writing anything

For each screenshot, work out:

- **Where in the system it is** — window/page title, breadcrumb, the highlighted
  item in the navigation
- **The exact click path** — write it the way a person follows it:
  `Meny → Ansatt → Fravær → Registrer`. Use the labels exactly as they appear,
  including Norwegian spelling and capitalisation.
- **Field and button names**, verbatim
- **Any error code or message**, verbatim

If the images are in sequence, treat them as one workflow from start to finish.

## 2. Only write what you can actually see

This is the rule that makes the knowledge base trustworthy.

- If a label is blurry, cut off, or ambiguous — **do not guess it**. Write
  `[uklart: <what you think it might be>]` and list it under "Needs checking".
- If a step is clearly missing between two screenshots, say so rather than
  inventing the bridge.
- Never fill in a menu you'd "expect" a system like this to have.

A procedure with three verified steps and one honest gap is useful. One with a
plausible invented step is worse than nothing, because someone will follow it.

## 3. Strip personal data — always

These screenshots come from HR and school systems. Before writing anything:

- Never record names, birth numbers, addresses, email addresses, phone numbers,
  employee or pupil numbers, or case references
- Describe people by role: "en ansatt", "en elev", "en bruker i lærerrollen"
- If a specific record's data is needed to make a step make sense, write
  `<navn>` or `<elev>` as a placeholder
- If a screenshot contains personal data you don't need, just don't mention it

Do not refuse to work with the screenshot because it contains personal data —
read it, use the structure, leave the data out of what you write.

## 4. Pick the right file

- Visma Enterprise (HR/lønn/ERP) → `skills/visma-enterprise/SKILL.md`
- Visma InSchool (skole, elever, timeplan) → `skills/visma-inschool/SKILL.md`
- Entra/Azure/lisenser → `skills/entra-azure/SKILL.md`
- Outlook/Teams/OneDrive/Office → `skills/m365-office/SKILL.md`
- Windows/enhet/skriver → `skills/windows-endpoint/SKILL.md`

If it fits none of them, propose a new skill folder and ask before creating it.

## 5. Show it, then save it

The screenshots you were sent are stored permanently as documentation assets —
the message tells you their names. **Reference them in the procedure**, right
after the step each one illustrates, so the next person sees the actual screen:

    ![<kort beskrivelse>](asset:<navnet du fikk oppgitt>)

Write the entry in this format and show it to the user **before** saving:

```
### <Kort tittel på oppgaven>
**Gjelder:** <hvem/hvilken rolle>
**Slik gjør du:**
1. <steg>
   ![<beskrivelse>](asset:<navn>)
2. <steg>
**Merk:** <fallgruver, tidsforsinkelser, hva som ser feil ut men er normalt>
**Verifisert:** <dagens dato>
```

Ask: *"Ser dette riktig ut? Er det noe jeg har misforstått?"*

List anything under **Needs checking** that you marked `[uklart]`, and ask about
those specifically — that's the user's chance to fill the gaps cheaply.

When they confirm, append it under **Verified procedures** in the chosen file
using Edit. Do not overwrite existing entries; add to them. If an entry for the
same task already exists, propose merging rather than duplicating.

## 6. Close the loop

Tell them in one line what you saved and where. If the workflow revealed
something about how systems connect — a sync, a schedule, an upstream owner —
offer to add it to that file's **Integration notes** as well. That section is
worth more than any single procedure.
