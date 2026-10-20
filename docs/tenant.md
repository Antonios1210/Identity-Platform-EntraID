# Tenant

- Tenant: Contoso / lodsm017097.onmicrosoft.com
- Tenant ID: e445685f-b255-4c83-962e-ac52b06839f4
- Source: Skillable SC-300 lab. Lab access expires 2027-02-23.
- Persistence: survives as long as the lab is NOT submitted/completed
  and is resumed regularly. Launch daily.
- Licenses: EMS E5 (20 seats, 0 assigned) = Entra ID P2.
  Office 365 E5 no Teams (20 free) - use for group-based licensing demo.
  Office 365 E3 is 19/20 assigned - do not touch.
- Baseline 2026-10-20: 33 users, 31 groups, 8 apps, 210 SPs,
  2 CA policies, 0 administrative units, 1 EM catalog, 1 access package.
- Engine app registration: identity-sync-engine
- workerId extension: extension_2fab4beb26ce4d2ba0591903ed9f7c9b_workerId
- Known gap: accessReviewDefinitions returns 403.
  Needs AccessReview.Read.All - add in Phase 4.
