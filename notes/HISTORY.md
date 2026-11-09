# Historical design and deployment records

Use [Architecture](../ARCHITECTURE.md), the [collector guide](../collector/README.md),
the [Worker guide](../worker/README.md) and [Tradier operations](../collectors/tradier/OPERATIONS.md)
for current navigation. The following immutable records preserve the detailed
reasoning and evidence that used to sit at the repository root:

- [Product design at the pre-cleanup revision](https://github.com/OwenTanzer/live-options-view/blob/646e61e4c3c0df71006519eaded1d8c53c8d55a7/DESIGN.md): protocol notes,
  schemas, display conventions, implementation history and rollout proposals.
  It mixes multiple eras; its old Tradier start/restart instructions and bot-index
  migration sequence are superseded. Its June date does not date every later addition.
- [Tradier deployment history and optional-IBIT proposal](https://github.com/OwenTanzer/live-options-view/blob/646e61e4c3c0df71006519eaded1d8c53c8d55a7/TRADIER_DEPLOYMENT.md):
  September receipts, capture requirements, capacity observations and proposed
  enablement steps. Old appointments and pending labels are historical statements,
  not newly assigned work or proof of current enablement.

These records remain in Git rather than being copied into another maintained
manual. Current specifications live in [specs/](specs/README.md); their former
root-level compatibility pointers have been removed. External links to those old
branch-relative paths must use the maintained location or an immutable revision.
