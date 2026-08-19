"""Check topic classification and knowledge coverage against the real workspace.

The unit tests use a synthetic workspace, so they cannot tell you whether the
classifier actually recognises the way people here write. This can: it prints what
each skill holds, and how a set of realistic questions gets routed.

    python scripts/probe_coverage.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from core.knowledge import (classify_topic, coverage_report,  # noqa: E402
                            skill_coverage)

# Written the way the two audiences actually write — pupils in plain Norwegian,
# staff in product terms. Expected topic on the left of the tuple.
QUESTIONS = [
    ("frid-nettverk", "student", "Jeg får ikke koblet mobilen min på skolens wifi"),
    ("frid-nettverk", "student", "Hvordan aktiverer jeg kontoen min?"),
    ("windows-endpoint", "student", "PC-en min er kjempetreg og henger hele tiden"),
    ("windows-endpoint", "staff", "Skriveren på personalrommet skriver ikke ut"),
    ("m365-office", "student", "Outlook viser ikke de nye e-postene mine"),
    ("m365-office", "staff", "En bruker mangler tilgang til et delt SharePoint-område"),
    ("visma-inschool", "student", "Jeg ser ikke timeplanen min i VIS"),
    ("visma-inschool", "staff", "Karakterene mangler for en klasse i Visma InSchool"),
    ("visma-enterprise", "staff", "Hvordan registrerer en leder egenmelding for en ansatt?"),
    ("entra-azure", "staff", "Bruker får feilkode AADSTS50076 ved innlogging, lisens ok"),
    ("entra-azure", "staff", "Jeg må resette MFA for en bruker"),
    (None, "student", "hei"),
]


def main() -> None:
    print("=== what each skill holds ===")
    for skill, facts in sorted(skill_coverage().items()):
        state = "documented" if facts["documented"] else "NOTHING DOCUMENTED"
        print(f"  {skill:<20} {facts['chars']:>6} chars  "
              f"{facts['procedures']} proc  {facts['anchors']:>2} anchors  {state}")

    print("\n=== classification ===")
    wrong = 0
    for expected, tier, question in QUESTIONS:
        got = classify_topic(question, tier)
        ok = got == expected
        if not ok:
            wrong += 1
        print(f"  {'ok ' if ok else 'MISS'} [{tier:<7}] {got or '—':<18} {question}")
        if not ok:
            print(f"       expected {expected or '—'}")

    print("\n=== report, as the admin view would show it ===")
    counts: dict[str, int] = {}
    for expected, tier, question in QUESTIONS:
        topic = classify_topic(question, tier)
        if topic:
            counts[topic] = counts.get(topic, 0) + 1
    for row in coverage_report(counts):
        flag = "  <-- write this next" if row["undocumented_demand"] else ""
        print(f"  {row['skill']:<20} asked={row['asked']:<3} "
              f"proc={row['procedures']} anchors={row['anchors']:<3} "
              f"documented={str(row['documented']):<5}{flag}")

    print(f"\n{len(QUESTIONS) - wrong}/{len(QUESTIONS)} classified as expected")
    if wrong:
        print("Add the missing words to ALIASES in core/knowledge.py.")


if __name__ == "__main__":
    main()
