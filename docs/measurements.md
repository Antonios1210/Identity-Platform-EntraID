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
