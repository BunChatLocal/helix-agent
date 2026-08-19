"""Dump the visible text of the FRID onboarding portal.

Used to turn the portal's guides into a pupil-facing skill file, so the agent
quotes the real onboarding steps instead of guessing at them.
"""
import html
import pathlib
import re
import urllib.request

URL = "https://onboard.frid-iks.no/"

with urllib.request.urlopen(URL, timeout=20) as resp:
    raw = resp.read().decode("utf-8", errors="replace")

text = re.sub(r"(?s)<script.*?</script>", " ", raw)
text = re.sub(r"(?s)<style.*?</style>", " ", text)
text = re.sub(r"(?s)<!--.*?-->", " ", text)
text = re.sub(r"<br\s*/?>", "\n", text)
text = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", text)
text = re.sub(r"<[^>]+>", " ", text)
text = html.unescape(text)

lines = [re.sub(r"\s{2,}", " ", line.strip()) for line in text.split("\n")]
text = "\n".join(line for line in lines if line)

out = pathlib.Path(__file__).with_name("onboard_portal.txt")
out.write_text(text, encoding="utf-8")
print(f"wrote {out} ({len(text)} chars)")
