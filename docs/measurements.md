# Measurements

Real numbers from the lab tenant. No estimates.

## 2026-10-20 - group creation and licensing

- Directory replication lag: a POST /groups returns 201 with an id,
  but POST /groups/{id}/assignLicense against that id returned 404
  Request_ResourceNotFound on the immediate next call. Succeeded on
  retry ~30s later. The engine must retry on 404-after-create rather
  than trusting a 201.

- License pool at baseline: Office_365_E3_(no_Teams) 19 of 20 free.
  EMSPREMIUM (Entra ID P2) 0 of 20 free - all consumed by prebuilt
  Contoso users. Tenant-level P2 features work; assigning P2 to a
  synthetic worker requires freeing a seat first.

## Pending

- Dynamic group evaluation latency (attribute set -> membership)
- Group-based licensing propagation (membership -> license assigned)

## 2026-10-20 17:07 UTC - dynamic membership and licensing latency

Probe user created with department=Engineering, usageLocation=US,
and the workerId extension set. Target group IDP-Dept-Engineering.

| Event | Elapsed from user creation |
|---|---|
| Dynamic group membership evaluated | 42s |
| Group-based license assigned | 42s |

Licensing lagged membership by 0s.

## 2026-10-20 17:11 UTC - dynamic membership and licensing latency

Probe user created with department=Engineering, usageLocation=US,
and the workerId extension set. Target group IDP-Dept-Engineering.

| Event | Elapsed from user creation |
|---|---|
| Dynamic group membership evaluated | 42s |
| Group-based license assigned | 53s |

Licensing lagged membership by 11s.

## 2026-10-20 17:15 UTC - dynamic membership and licensing latency

Probe user created with department=Engineering, usageLocation=US,
and the workerId extension set. Target group IDP-Dept-Engineering.

| Event | Elapsed from user creation |
|---|---|
| Dynamic group membership evaluated | 42s |
| Group-based license assigned | 43s |

Licensing lagged membership by 0s.

### Summary across three runs

| Run | Membership | License | Gap |
|---|---|---|---|
| 1 | 42s | 42s | within same poll window |
| 2 | 42s | 53s | 11s |
| 3 | 42s | 43s | 1s |

Membership evaluation landed at 42s on all three runs. Licensing is
strictly downstream of membership and lagged it by 1-11s. All observed
variance is in licensing, not in rule evaluation - consistent with
dynamic membership running on a fixed cycle and license provisioning
being a separate job with independent timing.

### Caveats

- Poll interval is 10s, so every figure carries +/-10s resolution.
- Tenant is small: 33 users at baseline. Rule evaluation scales with
  directory size and rule complexity. Treat these as a floor.
- Rule under test combines a department equality check with a
  directory-extension not-null check.

### Why this matters

This is the floor on revocation latency for any design where group
membership is the control plane. A termination cannot reach a
downstream app faster than the dynamic rule re-evaluates, because
membership is what drives provisioning. Measured at 42s here, plus
license propagation on top.

## Correction (2026-10-20, same day) - audit log supersedes polling

The Entra audit log gives server-side timestamps for each license
assignment. Creation-to-license lag measured from it:

| User | Lag |
|---|---|
| probe run 1 | 32.5s |
| probe run 2 | 48.9s |
| probe run 3 | 38.6s |
| 5 engineers, created within 7s | 15.6s, 16.5s, 23.1s, 88.8s, 92.6s |

The earlier claim that membership consistently evaluated at 42s is
withdrawn. Run 1's license landed at 32.5s, before polling first
observed membership at 42s; since licensing depends on membership,
membership resolved earlier than polling reported. The repeated 42s
was a polling artifact, not an Entra evaluation cycle.

Under a burst of five creates, lag spread from 16s to 93s - consistent
with queued processing. Treat ~15-95s as the observed range.

### Assignment path is mislabeled in this tenant

The portal and Graph licenseAssignmentStates both report these
licenses as Direct (assignedByGroup null). The audit log shows every
license was assigned by CloudLicensingSystem, Microsoft's group-based
licensing processor. The engine's service principal performed no
license operations. Confirmed behaviorally: moving W0012 out of
Engineering removed the license, actor CloudLicensingSystem.
Excerpt: docs/evidence/phase1-license-audit-excerpt.csv

### Instrument note

Server-side audit timestamps beat client polling for latency work:
sub-second precision, no observer effect. Use them for Phase 5.

## 2026-10-22 - Phase 2: first Entra provisioning cycle into the SCIM app

Times from the SCIM app's own request log (UTC).

| Event | Time |
|---|---|
| Provisioning turned on in Entra | 20:22 |
| First create (delphine.okonkwo) | 20:22:14.70 |
| Last create (j.park) | 20:22:16.70 |

The initial cycle started within seconds of enabling provisioning and
created 12 users in about 2 seconds. Each create was preceded by a
GET /Users?filter=userName eq "..." existence check. Requests came from
40.126.4.44 (Microsoft provisioning service) through the Cloudflare tunnel.

The "every 20-40 minutes" interval in the portal applies to later
incremental cycles, not the first one. That interval is what bounds
revocation latency to the app, and is still to be measured.

Two workers were not provisioned: W0011 (pre-hire) and W0013 (on leave),
both disabled in Entra. Reason to be confirmed from provisioning logs.

Confirmed from provisioning logs: W0011 skipped with SkipReason
NotEffectivelyEntitled ("not active in the source system"). Assigned to
the app = True, IsInProvisioningScope = True, IsActive = False. Entra does
not create app accounts for disabled users even when they are in scope.
A pre-hire's app account is therefore created on the first cycle after
the engine enables them on their start date.

## 2026-10-22 - Phase 2: leaver end to end (W0010, Victor Almeida)

App-side timestamps from the SCIM app's own log; Entra-side from audit
and provisioning logs. All UTC.

| Hop | Time | From T1 |
|---|---|---|
| T0: HR terminates W0010 | 20:42:49 | |
| T1: engine disables Entra account, revokes sessions | 20:43:45 | 0 |
| Provisioning cycle 1 attempts update, fails | 21:02:23 | 18m 38s |
| T2: app deactivates account (soft delete) | 21:20:03 | 36m 18s |

Result: terminated contractor removed from the app with no manual
action in the app. Account soft-deleted, history retained.

Cycle 1 failed on an app defect: the SCIM app stored the enterprise
'manager' attribute as a bare string, as Entra sent it in PATCH, instead
of the complex {"value": id} that RFC 7643 requires. On the next read
Entra could not deserialize the user, failed the match step
(SystemForCrossDomainIdentityManagementServiceIncompatible), and never
sent active=false. The app logged 200 OK throughout; only the Entra
provisioning log showed the failure. It affected every user with a
manager (12 of 13), so terminations were silently blocked for nearly
everyone. Fixed by normalizing manager on write and on read.

Real-world exposure window: 36m 18s, doubled by one integration defect.
Without the defect, deactivation would have landed on cycle 1, ~18.6
minutes after T1. Observed interval between cycles: ~18 minutes.
A clean leaver with no defects is still to be measured.

Victor kept his E3 license after termination: the Engineering dynamic
rule checks department and workerId, not accountEnabled.
