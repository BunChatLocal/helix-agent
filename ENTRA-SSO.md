# Microsoft Entra SSO — setup

Staff and pupils both sign in at `/chat` with their existing school or work
account. Each person automatically gets their own private conversations, keyed on
their Entra object ID. No codes, no passwords, nothing to remember or lose.

**The one fact that shapes everything below: they are not in the same directory.**
FRID employees sign in from the FRID tenants, pupils from Akershus fylkeskommune's.
The `tid` claim in the token is what separates them, and it is what decides whether
someone is treated as staff or as a pupil — see `ROLES.md` for the permission model
that hangs off it.

| Tier | Tenant | Domain |
|---|---|---|
| Staff | `9c3d6c7b-385f-40cb-bf07-1edf937af70d` | `frid.no` |
| Staff | `fe578cc6-ca56-451f-a297-08c5b8011b0d` | `frid-iks.no` |
| Pupil | `4d6d8a90-10fd-4f78-8fc1-5e28844e0292` | `afk.no` |

Any other tenant is refused at the callback. Østfold and Buskerud pupils exist and
are deliberately out of scope; admitting them means adding their tenant id to
`tiers.student_tenant_ids`.

Tenant ids were read from
`https://login.microsoftonline.com/<domain>/v2.0/.well-known/openid-configuration`
— the `issuer` field carries the directory id. Re-check there if a domain moves.

## Who has to do what

Two organisations are involved, and only the first step is FRID's to take.

| Step | Who | Blocking? |
|---|---|---|
| 1. Create the app registration | app-registration rights in **FRID IKS** | yes |
| 2. Let Akershus accounts consent | possibly an **Akershus** admin | yes, for pupils |
| 3. Configure and switch on Helix | you | no |

Staff sign-in works as soon as step 1 is done. Pupils need step 2. Step 2 is the
one with external lead time, so start asking early — but read it first, because it
may turn out to need nothing at all.

## 1. Register the application — FRID IKS tenant admin

Entra admin centre → **App registrations** → **New registration**:

- **Name:** Helix IT Support
- **Supported account types:** **Accounts in any organizational directory
  (Any Microsoft Entra ID tenant — Multitenant)**
- **Redirect URI:** platform **Web**, value
  `https://<hostname>/api/auth/microsoft/callback`

> **Multitenant is not optional, and this is the easy mistake to make.** The
> obvious choice — *Accounts in this organizational directory only* — produces a
> registration that works perfectly for staff and that **no pupil can ever sign in
> through**, because their account lives in a different directory. You would not
> find out until a pupil tried. If the registration already exists as
> single-tenant, this is changeable afterwards under
> **Authentication → Supported account types**.

Do **not** add personal Microsoft accounts. "Any organizational directory" means
work and school accounts only; the tenant allow-list in Helix then narrows that to
the three tenants above.

**Redirect URIs.** The value must match Helix's config character for character,
including scheme, host, port and path. Add both of these now if the production
hostname isn't settled — a registration can hold several:

```
http://localhost:18791/api/auth/microsoft/callback     (local testing)
https://<hostname>/api/auth/microsoft/callback          (production)
```

Then from **Overview**, note:
- **Application (client) ID**
- **Directory (tenant) ID** — FRID's own, the tenant the app lives in

Under **Certificates & secrets** → **New client secret** → copy the *Value*
immediately; it is shown once. Note its expiry date somewhere you will see it
again, because sign-in stops working the day it lapses.

Under **API permissions**, the default **`User.Read` (delegated)** is all Helix
uses. It reads the signed-in person's own name and email and nothing else. No
directory-wide permission, no application permission, no mail or file access.

## 2. Let Akershus accounts consent

Because the app is registered in FRID's tenant and pupils arrive from Akershus's,
Akershus has to allow it. There are two ways that happens, and the cheap one may
be enough:

**Try user consent first.** `User.Read` is a low-impact delegated scope, so if
Akershus permits users to consent to apps for such scopes — the Entra default —
the first pupil to sign in simply gets a one-time "Helix IT Support wants to sign
you in and read your profile" prompt, accepts it, and that's the whole step. Test
this with a single pupil account before escalating anything.

**Admin consent, if user consent is turned off.** Public-sector tenants often
disable user consent. In that case the pupil sees *"Need admin approval"* and an
Akershus administrator has to grant it once for the whole tenant, by opening:

```
https://login.microsoftonline.com/4d6d8a90-10fd-4f78-8fc1-5e28844e0292/adminconsent?client_id=<application-client-id>
```

signing in with an Akershus admin account and accepting. It grants `User.Read`
delegated for pupils who choose to use Helix — sign-in and own-profile only. It
gives FRID no access to Akershus data, no ability to read anything about pupils who
never sign in, and no application permissions. That is the whole of the ask, and
saying so plainly is what makes it answerable.

If Akershus declines a multitenant app on principle, the alternative is a second
registration in their tenant, which Helix would need config changes to support —
two client ids. Worth knowing as a fallback, not worth building until asked.

## 3. Configure Helix

Store the secret encrypted — never in `config.json`:

```powershell
cd C:\Users\jorgenthin_f\helix-agent
.\.venv\Scripts\python.exe main.py secrets set ENTRA_CLIENT_SECRET "<the secret value>"
```

Then edit `~/.helix/config.json`:

```json
"entra": {
  "enabled": true,
  "tenant_id": "<FRID directory tenant id>",
  "client_id": "<application client id>",
  "redirect_uri": "https://<hostname>/api/auth/microsoft/callback",
  "restrict_to_tenant": true,
  "allowed_group_ids": []
},
"tiers": {
  "staff_tenant_ids": [
    "9c3d6c7b-385f-40cb-bf07-1edf937af70d",
    "fe578cc6-ca56-451f-a297-08c5b8011b0d"
  ],
  "student_tenant_ids": [
    "4d6d8a90-10fd-4f78-8fc1-5e28844e0292"
  ],
  "staff_domains": ["frid.no", "frid-iks.no"],
  "student_domains": ["afk.no"]
}
```

The `tiers` block matches the defaults compiled into `core/config.py`, so Helix
behaves correctly without it. Write it out anyway: it puts the tenant ids somewhere
reviewable, and it is where you would add a county without touching code.

Once more than one tenant is listed, `_entra_authority` switches from the
single-tenant endpoint to `organizations`, because no single-tenant endpoint can
serve two directories.

> **Never set `restrict_to_tenant` to `false` to "make multi-tenant work".** It
> makes the authority fall back to `organizations` *and* skips the tenant check,
> which admits any work or school account in the world. The `tiers` allow-list is
> what makes the multi-tenant endpoint safe, and it must stay explicit.

`redirect_uri` must match the app registration **character for character**.
Restart Helix afterwards.

## 4. Verify

```powershell
curl http://127.0.0.1:18791/api/auth/microsoft/enabled     # {"enabled":true}
```

Then sign in twice and confirm the tier follows the account, not the page:

1. A **FRID** account → commands work, no subject restriction.
2. An **Akershus pupil** account → slash commands do nothing, and a homework
   question is refused with *"Jeg kan bare hjelpe med IT-ting her."*

If sign-in fails, `~/.helix/logs/helix.log` names the reason, and the redirect
carries a code:

| Redirect | Meaning |
|---|---|
| `/?login_error=2` | tenant not on the allow-list — expected for any tenant but the three above |
| `/?login_error=1` | audience mismatch, token exchange failure, or foreign tenant under the legacy single-tenant path |

A pupil hitting `login_error=2` when they should be admitted means the Akershus
tenant id is wrong or missing from `student_tenant_ids`.

## Before it is reachable by anyone else

1. **Test sign-in off.** Set `web.dev_login` to `false`. Enabling `entra` already
   disables it in code — `_dev_login_available` requires `not entra.enabled` — but
   leaving the flag set means one config edit stands between the site and
   unauthenticated impersonation.
2. **TLS.** OAuth over plain HTTP leaks tokens. Terminate TLS at a reverse proxy
   (IIS, nginx, Caddy) and use the `https://` redirect URI.
3. **Bind address.** `web.host` should be `127.0.0.1` with the reverse proxy in
   front, not `0.0.0.0`. It is currently `0.0.0.0`, which is only survivable
   because no inbound firewall rule exists for 18791.
4. **The agent has no shell.** `--allowedTools` in `core/cli_backend.py` omits
   Bash for every tier, and pupils get no tools at all. Keep it that way.
5. **Quota.** Every conversation runs on one Claude subscription. Tiered rate
   limits soften this but do not remove it: when the quota goes, it goes for
   everyone at once.
6. **GDPR.** Pupils are minors and transcripts are written to `~/.helix/sessions`.
   This needs a proper assessment before it handles real cases.

## How it works

- Authorization-code flow with **PKCE**; `state` is single-use and expires after
  10 minutes (CSRF protection)
- The returned `id_token` is checked for **audience** and then for **tenant**
  against the allow-list, before any session is created. An unlisted tenant gets no
  tier and no entry — personal accounts and foreign tenants are refused.
- Helix mints its own JWT carrying `sub = entra:<oid>`, `role = user`, the tier,
  and the email
- The websocket derives the session key from that `sub` claim — **never** from
  anything the client sends — so one person cannot read another's conversation

The tier is fixed when the token is minted and again when the conversation starts.
It belongs to the person, and a person does not change mid-conversation.

## Two pages, three tiers

| Page | Who | Login |
|---|---|---|
| `/` | you | web password |
| `/chat` | staff and pupils | Microsoft SSO |

| | Admin | Staff | Pupil |
|---|---|---|---|
| How | web password | SSO, FRID tenant | SSO, Akershus tenant |
| `role` claim | `admin` | `user` | `user` |
| Chat | yes | yes, own conversations | yes, own conversations |
| Slash commands | yes | yes | **no** |
| Agent tools | full | full, never Bash | **none** |
| Subject scope | any | any | IT only |
| Management endpoints | yes | **403** | **403** |

Every management endpoint — 39 at the time of writing, counted by
`Depends(require_admin)` — requires `role=admin`; a staff or pupil token is
rejected, verified by test rather than by intent. Admin is still the shared web
password — moving it to an allow-list of named FRID identities is planned, not
built. `ROLES.md` has the whole model.

Each person can hold several conversations. The session key is
`<sub>|<conversation-id>`, where `<sub>` always comes from the verified token and
the conversation id must be a UUID — so a client can only ever address its own
namespace. Verified: two users opening the *same* conversation id get separate
sessions and cannot see each other's messages.

## Testing without Entra

Until the app registration exists, `web.dev_login` lets you sign in by typing any
email address. The domain decides the tier — `@frid.no` or `@frid-iks.no` gives
staff, `@afk.no` gives a pupil, anything else is refused — so the tiering is
exercised in testing rather than first meeting reality on the day SSO goes live.

**This verifies nothing about identity.** Anyone reaching the endpoint can become
anyone. Guards:

- off unless `web.dev_login` is explicitly `true`
- **refused outright whenever `entra.enabled` is true**, so it cannot survive the
  switch to real SSO — verified by test
- every use is logged as `TEST SIGN-IN (unverified)`
- the page shows a permanent yellow TESTMODUS bar so it is never invisible

## Data retention

Conversations untouched for their retention window are deleted — the session row
*and* the transcript file. A job runs daily at 03:30, plus once at startup so
downtime doesn't leave expired data behind.

| | Window | Config |
|---|---|---|
| Staff | 30 days | `session.retention_days` |
| Pupil | 5 days | `session.student_retention_days` |

Pupils get the shorter clock because they are minors and the conversation is
usually one fixed problem. `0` disables purging entirely and logs a warning on
every start; `student_retention_days: 0` falls back to the staff window.

### Expiry warnings

The sidebar warns as deletion approaches, scaled to the window rather than fixed —
40% of it for the amber warning, 15% for red — so a pupil on 5 days is warned at 2
days and 1 day rather than being told about a deadline that has already passed.

Clicking the dot offers **Behold samtalen** or **Ignorer**. What "keep" does
depends on who is asking:

- **Staff** get a handover briefing and a fresh context window, and the clock resets.
- **A pupil with a short conversation** just gets the clock reset — no model call,
  because summarising three turns produces nothing shorter than what is already there.
- **A pupil with a substantial one** (5+ of their own messages, or 4,000+
  characters) gets a short Norwegian note of *the solution* appended to their own
  thread, and the clock resets. It stays in their thread and goes nowhere else.

A conversation cannot be kept until the agent has actually **answered** something,
which is not the same as having exchanged a few messages. The agent is designed to
ask one clarifying question first, so a two-message thread is usually "vague problem
→ clarifying question" and has nothing worth retaining; conversely one good answer
with steps is worth keeping even if the thread is short. `_is_substantive_answer`
counts an assistant message as an answer if it contains a code block or two or more
list items, or runs past 240 characters — and never if it is short and ends in a
question mark. The UI shows a notice rather than offering a button that fails.

## Images

Staff and pupils can both paste a screenshot into the message box (Ctrl+V), drag
one in, or use the 🖼 button. PNG/JPEG/GIF/WebP, 5 MB max.

Uploads are validated by magic number, not the declared content type. The API
returns an opaque id and the server keeps the id→path mapping, so a client can
never name a path or point the agent at another file. Ids are owner-checked, capped
at 4 per message, and swept after 6 hours — both the in-memory map and the temp
directory itself, by file age, so a restart cannot orphan them.

What happens next differs by tier:

- **Staff** images are passed by path, which is what lets the agent open one and
  turn it into permanent documentation via `/dokumenter`.
- **Pupil** images are inlined into the message as bytes, because a pupil's agent
  has no file tools. Nothing a pupil sends is ever persisted into the knowledge
  base — commands are staff-only, and that is the whole mechanism.

Reading documentation images back is also tiered: `/api/assets/{name}` serves a
pupil only the images their own guides cite, and 404s the rest. Without that, a
pupil could fetch the internal Azure and MFA-reset screenshots by name.

## Rate limits

Per person, across all their open tabs — not per connection, which would have made
the limit resettable by closing one.

| | Per minute | Per hour |
|---|---|---|
| Staff | 20 | 200 |
| Pupil | 8 | 60 |

Under `security.rate_limit` as `per_minute` / `per_hour` and
`student_per_minute` / `student_per_hour`. A pupil limit of `0` falls back to the
staff numbers. Rejections are audited once per person per five minutes, so a
hammering tab leaves one record rather than hundreds.

## Context limit

`session.context_limit_pct` (default **0.75**). Once a conversation fills that
share of its usable context window, the server refuses further turns and the UI
shows a **Komprimer samtalen** button. Compaction summarises the thread with Haiku
and continues from the summary.

This is a cost control: without it every message re-sends the whole history, so a
long thread gets steadily more expensive per turn. The check runs *before* the
model is called, so a blocked turn spends nothing. The meter is green → amber
(55%) → red (75%) so people see it coming.

## Surviving updates

Conversations live in `~/.helix/sessions/` — outside the repo, so `git pull` and
code changes never touch them. Verified: conversations and their message counts are
identical across a full stop/start.

The remaining risk is a bad schema migration or corruption, so `sessions.db` is
snapshotted into `~/.helix/sessions/<agent>/backups/` on every startup, keeping the
last 7 days. It uses SQLite's backup API, which copies a consistent snapshot even
mid-write; a plain file copy would not. A backup failure logs a warning and never
blocks startup.

To restore: stop Helix, copy a snapshot over `sessions.db`, start again.

`/chat` is served with `Cache-Control: no-store`, so after an update nobody is left
on a cached page talking to a changed API.
