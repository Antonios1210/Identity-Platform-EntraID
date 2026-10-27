# Phase 1: HR to Entra

> **Status: skeleton.** Prompts in *italics* are for me to replace with my own words at the end of Project 1. The bullets under "What broke" are notes, not final text: rewrite them in my own voice. The numbers and evidence lists are facts from the build and can stay.

---

## The problem

*In 3 or 4 sentences: what goes wrong when IT creates, changes and removes accounts by hand from HR tickets? What happens on someone's first day, when they change departments, and when they're fired?*

---

## What I built

| Piece | What it is |
|---|---|
| Mock HR system | FastAPI + SQLite. The source of truth. Snapshot and delta endpoints, seeded with deliberate traps |
| Sync engine | Reads HR, reads Entra, computes the difference, and applies it through Microsoft Graph |
| bootstrap-identity.py | Creates the engine's app registration, permissions, admin consent, and the workerId extension |
| bootstrap-tenant.py | Builds the department dynamic groups, the birthright group, and group-based licensing from config |
| export-tenant.py | Snapshots the tenant; manifests go into git |
| measure-latency.py | Times dynamic group membership and group-based licensing with a probe user |

*One paragraph: how the pieces connect, from an HR change to a licensed Entra account.*

---

## Design decisions

For each one: what I chose, what I rejected, and why, in my own words.

### 1. Two IDs: worker_id and employee_id
*Why the anchor can't be the employee ID. Gregory and Nadia share 100014; Victor has none.*

### 2. The workerId directory extension as the anchor in Entra
*Stamped on create, read back every run. Invisible in the portal, which matters for helpdesk.*

### 3. Dynamic groups and group-based licensing as the control plane
*Why my code never touches group membership or licenses for a mover.*

### 4. A workerId clause in every dynamic rule
*What happens to the 20-seat license pool without it.*

### 5. Plan before apply
*Why the engine never writes without showing me first. What it caught.*

### 6. Everything scripted, nothing clicked in the portal
*Why, for a lab tenant that can reset. How long a rebuild takes.*

### 7. Ambiguous correlation halts instead of guessing
*My surgeon example.*

### 8. Usernames from a fixed preference list
*initial.last, then first.last, then numbered. Why "taken" includes names handed out in the same run. Why the order must never change between plan and apply.*

### 9. The delta cursor comes from HR's clock, not mine
*Why the engine replays HR's server_time instead of reading its own clock.*

---

## Results

| Measurement | Value | Source |
|---|---|---|
| Initial load | 44 operations (15 creates, 14 managers, 15 group adds), 0 failed | Engine audit log |
| Second run after the load | 0 operations | Engine plan |
| Group-based license, probe user (3 runs) | 32.5s, 48.9s, 38.6s | Entra audit log |
| Group-based license, 5 users created in 7s | 15.6s, 16.5s, 23.1s, 88.8s, 92.6s | Entra audit log |
| Mover W0012, Engineering to Sales | 1 attribute changed by the engine; license removed by Entra on its own | Engine trace, Entra audit log |
| New group addressable after create | 404 on the first call, fine ~30s later | bootstrap-tenant.py run |

*Two or three sentences: what these numbers mean. Why the burst was slower. Why audit timestamps beat polling.*

---

## What broke, and what surprised me

*For each: what happened, how I found it, what I changed. Pick the 4 or 5 strongest for interviews.*

- **The 42-second claim I had to withdraw.** Polling showed membership at 42s on all three runs. The audit log showed a license at 32.5s, which can't happen before membership exists. The 42s was a polling artifact.
- **"Direct" licenses that weren't.** Graph and the portal said Direct; the audit log said CloudLicensingSystem assigned them; Jun lost his the moment he left the group. Trust behavior and the audit actor. (Refined in Phase 2: the same group later produced a correct label.)
- **Orphan detection in delta mode.** A dry run flagged 14 live workers as orphans. A delta can prove someone changed, never that someone is gone. Only a full snapshot can.
- **Replication lag.** A group I'd just created returned 404 on the next call.
- **The license pool.** E5 and EMS were fully consumed by prebuilt users. The demo had to use E3.
- **A half-updated HR record.** Daniel's department moved but his cost center didn't.
- **Garbled names.** Zoë's name came out mangled through the console code page. The data was fine.

---

## Evidence index

**Engine**
- `phase1-audit-trace.jsonl`: all 44 operations of the initial load, one correlation ID
- `phase1-idempotency-rerun.txt`: the second run, 0 operations
- `phase1-mover-trace.jsonl`: the mover, one attribute changed

**Entra**
- `phase1-manager.png`: HR attributes and manager landed on Daniel
- `phase1-dynamic-group-members.png`, `phase1-dynamic-group-after-mover.png`: 5 members, then 4
- `phase1-mover-license-removed.txt`: Jun without a license
- `phase1-license-removed-by-group1.png`, `-2.png`, `-3.png`: actor, target, and E3 to empty
- `phase1-license-direct-label.png`: the misleading Direct label
- `phase1-license-audit-excerpt.csv`: every license change, actor CloudLicensingSystem

**Tenant**
- `exports/2026-10-20/_manifest.json`: baseline counts
- `docs/measurements.md`: all timings, including the correction

---

## Not done yet

- Mover and leaver into apps (done in Phase 2)
- Leave of absence: Rosa was created disabled. Is that right, or should a separate leave handler suspend her?
- Administrative unit scoping (needs two more Graph permissions)
- accessReviewDefinitions returns 403 (needs AccessReview.Read.All, Phase 4)
- Cost center mapped into Entra
- Certificate auth instead of a client secret
- Engine on a schedule instead of run by hand

---

## Interview questions to rehearse

*Answer each out loud, in my own words, before publishing.*

1. Why two IDs instead of just the employee ID?
2. What happens when two people want the same username?
3. Why plan and apply instead of writing changes directly?
4. How do you know running the engine twice won't create duplicates?
5. Why let dynamic groups assign licenses instead of your code?
6. How long does a new hire wait for a license, and how did you measure it?
7. Tell me about a result you had to retract.
8. Why does the engine stop instead of guessing when two accounts share a worker ID?
9. What goes wrong if you forget to follow Graph's page links?
