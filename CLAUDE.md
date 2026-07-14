## Architecture decisions — MANDATORY

@documents/decisions/README.md

### Before changing design or architecture
1. Check the ADR index above for any decision covering the area you're touching.
2. If one exists, READ the full ADR file before proposing changes.
3. If your change contradicts an accepted ADR, STOP. Say so explicitly and ask
   whether to supersede it. Do not silently work around it.

### After making a design or architecture change
You MUST record it. Trigger the `adr` skill and write a new ADR before
considering the task complete. This applies to:
- new services, dependencies, or third-party providers
- data model / schema changes beyond additive columns
- changes to agent topology or orchestration
- auth, tenancy, or compliance boundary changes
- anything that would be expensive to reverse

Trivial refactors, bug fixes, and styling do NOT need an ADR.