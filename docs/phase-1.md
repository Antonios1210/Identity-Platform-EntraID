\# Phase 1 — HR feed to Entra



\## Problem

(one paragraph: what breaks without an engine like this)



\## Design decisions

\- Two IDs, worker\_id vs employee\_id — why (W0014/W0015)

\- Dynamic groups + group-based licensing as the control plane — why

\- Plan/apply split — why

\- workerId clause in every dynamic rule — why (license pool)



\## What surprised me

\- 404 on a group I'd just created

\- 42s dynamic evaluation, consistent across runs

\- (anything else)



\## Evidence

\- docs/measurements.md

\- second run: zero operations

\- audit trace for one run



\## Not done yet

\- mover, leaver, leave of absence

\- certificate auth instead of a secret

\- administrative unit scoping

