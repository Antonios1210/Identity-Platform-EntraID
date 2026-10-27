# Phase 2: Entra to the apps

> **Status: skeleton.** Prompts in *italics* are for me to replace with my own words at the end of Project 1. The bullets under "What broke" are notes, not final text: rewrite them in my own voice. The numbers and evidence lists are facts from the build and can stay.

---

## The problem

*In 3 or 4 sentences: what goes wrong when accounts in downstream apps don't follow HR and Entra automatically? What happens to a terminated person's app access? Use a healthcare example.*

---

## What I built

| Piece | What it is |
|---|---|
| Contoso Portal | A SCIM 2.0 app. Entra's provisioning service creates, updates and disables accounts in it |
| Contoso Timeclock | A legacy-style app with its own REST API and no SCIM. Entra cannot provision it |
| Timeclock connector | My own code doing Entra's job for Timeclock: create, update, deactivate, read all, translate, correlate |
| Cloudflare quick tunnel | Gives the SCIM app on my laptop a public HTTPS address so Microsoft can reach it |

*One paragraph: how the pieces connect. Draw or describe the two paths from HR.*

---

## Design decisions

For each one: what I chose, what I rejected, and why, in my own words.

### 1. How Microsoft reaches the SCIM app: tunnel, not cloud hosting
*Why a tunnel was right for a lab, and what it cost me (URL changes on every restart, dies when the laptop sleeps).*

### 2. Leavers: soft delete, hard delete only after retention
*The wrongful-termination argument (ICU/ED clinician must be restorable immediately). Retention set by the longest applicable regulation; HIPAA's floor is 6 years. Why the purge job needs monitoring.*

### 3. externalId = workerId
*Why the app should carry the same permanent anchor as HR and Entra. What it enables later (three-way reconciliation).*

### 4. Group provisioning turned off
*Why. What would have happened if left on.*

### 5. Timeclock interface: its own REST API
*What I'd do if the app only had a database (JDBC) or a file import instead.*

### 6. Timeclock correlation: the engine keeps a link table
*Why the app couldn't store the worker ID, and what the link table costs me.*

### 7. Source of truth: HR grants, HR or Entra can revoke
*Why deny wins. The conflict this exposed: the engine re-enabling someone security disabled (fix planned for Phase 3).*

### 8. Lost links: re-link only when unambiguous
*Adopt automatically when exactly one account matches and nobody else owns it; otherwise flag for a human. Why not fully automatic, why not fully manual.*

---

## Results

| Measurement | Value | Source |
|---|---|---|
| First provisioning cycle after enabling | started within seconds; 12 creates in ~2s | SCIM app log |
| Provisioning cycle interval | 20m 26s, 19m 35s (~20 min) | SCIM app log probes |
| Leaver W0010, Entra to app | 36m 18s (doubled by the manager defect) | SCIM log, Entra logs |
| Leaver W0016 (clean), Entra to app | 18m 57s | SCIM log |
| Mover W0008, Entra to app | 18m 17s | SCIM log |
| Comparison W0012, HR to Timeclock (connector) | 23s | Timeclock deactivated_at |
| Comparison W0012, HR to Portal (Entra) | 16m 21s | SCIM log |
| Work done once a cycle starts | ~3s | SCIM log |

*Two or three sentences: what these numbers mean for revocation latency in practice. Revocation = engine interval + up to one provisioning cycle.*

---

## What broke, and what surprised me

*For each: what happened, how I found it, what I changed. Pick the 4 or 5 strongest for interviews.*

- **The manager bug.** The SCIM app stored `manager` as a bare string. Entra couldn't read users back and silently skipped every update for 12 of 13 users. The app logged 200 OK the whole time; only the Entra provisioning log showed it.
- **The engine disabling already-disabled people.** Found by a pre-commit plan check. My one-line fix.
- **The wrong-line edit.** My first fix attempt would have disabled 12 active employees. The plan step caught it before anything ran.
- **Tunnel troubleshooting ladder.** No response, then 404 (missing `/scim/v2`), then 401 (token dropped after editing the URL), then 200. Network, then path, then auth.
- **Entra skips disabled users** (`NotEffectivelyEntitled`). Pre-hires and people on leave never got app accounts.
- **The email ripple.** One department change went HR to engine to dynamic group to license to Exchange mailbox to app email. Jun kept a stale email after losing his license.
- **License labels.** The same group produced both "Direct" and group-assigned labels. Verified by behavior and the audit actor instead.
- **Terminated users keep their licenses.** The dynamic rule doesn't check whether the account is enabled.
- **Two kinds of garbled names.** Console code page 437, and an API response with no charset read as Latin-1. Both reversible; stored data was never wrong.
- **A network blip crashed the engine.** `graph.py` doesn't retry dropped connections. The Timeclock connector does.

---

## Evidence index

**Provisioning and joiner**
- `phase2-app-users-after-cycle.txt`: 13 users, 13 anchors
- `phase2-scim-requests-1.png`, `-2.png`: Microsoft searching, then creating
- `phase2-externalid-anchor.png`, `phase2-mapping-workerid.png`: the anchor decision, configured and working
- `phase2-manager-backfill.png`: manager wired using the app's own ID
- `phase2-skip-not-entitled.png`, `-leave.png`: disabled users skipped
- `phase2-connection.png`: how Entra reaches the app

**Leaver W0010 (Victor)**: `phase2-leaver-*` (10 files, including `phase2-leaver-scim-incompatible.png`, the manager bug)

**Clean leaver W0016 (Katerina)**: `phase2-leaver2-*` (9 files)

**Mover W0008 (Delphine)**: `phase2-mover-*`, including `phase2-mover-modified-properties.png` and `phase2-mover-email-ripple.txt`

**Timeclock and comparison W0012 (Jun)**: `phase2-timeclock-*`, `phase2-compare-*`

**Measurements**: `docs/measurements.md`

---

## Not done yet

- Security Hold marker so the engine stops undoing emergency disables (Phase 3)
- Link-table loss and recovery as a test (Phase 3)
- Terminated users keeping licenses (Phase 3 or 4)
- `graph.py` retry on dropped connections (Phase 5)
- Purge job monitoring (Phase 5)
- One orchestrated run with a shared correlation ID across connectors (Phase 5)
- Certificate auth instead of client secrets (Phase 5)
- Cost center mapped into Entra
- Group support in the SCIM app

---

## Interview questions to rehearse

*Answer each out loud, in my own words, before publishing.*

1. Walk me through what happens when someone is terminated, system by system.
2. Why soft delete instead of hard delete?
3. What's the difference between the SCIM client and the SCIM service provider?
4. How does Entra decide whether an account already exists in the app?
5. Tell me about a bug you found and how you found it.
6. When would you build a custom connector instead of using SCIM?
7. How do you know which app account belongs to which person when the app can't store your ID?
8. What's your source of truth, and what happens when HR and Entra disagree?
9. How long does it take to revoke access, and what controls that number?
