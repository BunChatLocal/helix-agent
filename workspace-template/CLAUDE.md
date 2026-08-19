# CLAUDE.md

Helix is an IT-support tool for FRID IKS and the schools FRID operates.

**Scope depends on who is asking, and those rules are not in this file.** The
harness loads `AGENTS.md` and `SOUL.md` for everyone, then layers a per-tier set
on top depending on who signed in. `STUDENT.md` holds the restriction that pupils
may ask about IT and nothing else; `STAFF.md` states that colleagues have no such
limit. Applying one set of scope rules to everyone would either let pupils ask for
essays or stop colleagues asking ordinary questions.

The two shared files carry only what holds in every conversation. Anything that
assumes tools, files or a knowledge base — the session-startup list, the
skill-routing table, memory, commands, handing over a command — lives in
`OPERATIONS.md`, which loads for staff and for internal work only. A pupil's
prompt should contain no instruction that has to be cancelled afterwards.

Nothing load-bearing should live only in this file. Claude Code may pick it up as
project memory, since the workspace is the working directory — but Helix invokes
the CLI with `--system-prompt`, which replaces the preamble that normally carries
project memory, so whether these lines reach the model is not something the
harness controls. Rules that must apply belong in a file the context engine loads
deliberately.

See `ROLES.md` in the helix-agent repository for the whole permission model.
