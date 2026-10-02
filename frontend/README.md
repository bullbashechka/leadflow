# Leadflow frontend

React, TypeScript, Vite and Ant Design provide the shared-password login and protected CRM
shell. Setup, checks and operational commands live in the [repository README](../README.md).
Product requirements live in [PRD](../PRD.md); API interfaces live in
[the implementation contracts](../docs/contracts.md).

Theme settings belong to `src/theme.ts`. The API client uses same-origin `/api/` requests
through Vite's local proxy. Production proxy deployment is a later stage.

`AuthBoundary` retains protected children in memory while access is locked. Forms use
`useCRMAccess().controller.runWithAccess` for protected operations and retain their own
draft, submission UUID and request snapshot. This helper never automatically retries a
mutation. A thrown `AccessInterruptedError` does not prove that an already sent save failed.

`npm test` uses Node's runner for API and access-controller tests. The root browser runner
starts isolated local services before `npm run test:browser`. Test fixtures are separate
from the production entry and do not create real leads.
