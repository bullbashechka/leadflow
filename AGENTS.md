# Repository instructions

## Role and communication

- The user owns product decisions. The agent owns implementation, quality, and maintainability within the agreed scope.
- Decide engineering questions independently. Ask about product choices that cannot be inferred, and before destructive actions, spending, or changes to who can access data. A request authorizes its own scope; do not ask again. A review or a question does not authorize edits.
- Talk to the user in Russian by default. Explain product effects and tradeoffs plainly. When the user must act, provide exact steps and the expected result.
- Write technical documentation and agent instructions in English. Product documents and tasks may be Russian.
- Aim for "80% of the way to [ASD-STE100](https://www.asd-ste100.org/STE_faq.html)" in replies and documentation. Apply its core writing principles pragmatically, without strict compliance with every rule or the controlled English dictionary. Adapt the principles to Russian; preserve natural wording and technical accuracy.
- Use the fewest words that preserve meaning, requirements, evidence, and necessary context. Remove repetition, filler, and explanations that do not help the user decide or act.
- Use short sentences, one topic per sentence, active voice, and simple words. Use one consistent term per concept. Write instructions as direct commands, one action per step.

## Project context

- Leadflow is an agency lead mini-CRM with Telegram-bot intake and a browser interface.
- Read `PRD.md` for product requirements and acceptance criteria; use `TASKS.md` for the agreed stack, priorities, dependencies, and delivery checklist.
- Verify implementation status from the repository. Do not describe planned features or unchecked tasks as completed.
- Preserve existing documents when scaffolding. Update `README.md` with actual setup and check commands once tooling exists; do not invent commands or claim checks have passed without running them.

## Scope and delivery

- Implement P0 tasks before optional P1 features, following dependencies in `TASKS.md`. Mark tasks complete only after verifying their results.
- Preserve the acceptance criteria and contracts in `PRD.md`. Do not expand scope to deferred features or present unavailable capabilities as working.

## Architecture and workflow

- Before multi-file work, outline the affected files, tests, and checks. Implement a complete, small slice of behavior.
- Read the relevant area guide and nearby implementation before editing. Prefer existing utilities and framework APIs.
- Keep validation and persistence rules shared between the Django API and Telegram bot. Avoid duplicating business rules in HTTP handlers, bot handlers, and UI components.
- Enforce authentication and permissions on the server. Client guards are only a user-experience aid.
- Fix defects where they originate and inspect affected callers. For API changes, check both server and client contracts; for authentication, check permissions and session behavior; for asynchronous work, check retries, idempotency, ordering, and cancellation where relevant.
- Keep changes minimal and complete. Do not add speculative abstractions, layers, queues, caches, or other infrastructure without a demonstrated need.
- Choose ordinary dependencies within the agreed stack independently. Discuss stack changes, paid services, and substantial operational consequences with the user.
- Verify the main behavior with the narrowest meaningful checks. Report what changed, checks actually run and their results, material risks, and any required user action.

## Repository discovery

- Use `$code-scout` for non-trivial repository discovery to delegate broad code reading to a lower-cost subagent scout and keep irrelevant file contents out of the primary agent's context.
- Read and follow the skill before dispatch. Give the scout concrete research questions and the absolute repository path; keep discovery read-only.
- Reuse a current scout report. Use focused local reads for small tasks or specific gaps rather than repeating broad discovery.
- If the skill or its selected model is unavailable, explain briefly and use focused local research; do not silently substitute models.

## TDD: RED → GREEN → REFACTOR

For new behavior and bug fixes, work in small, testable increments:

1. **RED:** Write a focused test for the required behavior or bug reproduction. Run it and confirm it fails for the expected behavioral reason, not because of broken setup or imports.
2. **GREEN:** Implement the smallest change that makes the test pass. Run the relevant tests; do not weaken assertions to make failures disappear.
3. **REFACTOR:** Improve clarity and structure without changing behavior. Rerun the relevant tests and keep them green.

- Derive tests from observable behavior and PRD acceptance criteria, not implementation details. Cover affected error paths and persistence or deduplication contracts where relevant.
- Reproduce bugs at the lowest level that demonstrates the defect. Use unit tests for pure rules and client logic; use integration tests with a test database for API behavior, permissions, and persistence.
- Verify database-dependent behavior against a test database. Choose isolation according to the test's purpose; fake external providers when needed.
- Add end-to-end journeys when lower-level tests cannot prove the client-server connection. Extend an existing journey when practical.
- Avoid assertions tied to incidental wording or implementation details. Test messages when they are part of a requirement. Prefer accessible roles and labels for UI selectors; use stable `data-testid` values when semantic selectors are insufficient.
- For a pure refactor, establish passing behavioral coverage first and keep it passing throughout.
- Documentation, copy, and styling changes do not need artificial test-first steps. Verify layout changes visually. For scaffolding or configuration, use appropriate startup, build, or configuration checks; state what was actually verified.
- Use the project's configured test tools once available. Report checks run, results, and any blockers honestly; never claim a RED or GREEN result that was not observed.

## UI verification

- Use Ant Design for standard UI controls, typography, feedback, and dialogs. Use its layout components before custom CSS. Do not add another component library or wrap standard components without a repeated need.
- Keep global theme settings in `frontend/src/theme.ts`. Use the library theme and public component APIs; do not override internal Ant Design selectors. Limit custom CSS to page geometry and responsive layout.
- Follow the agreed UI choices in `TASKS.md`. Implement loading, errors, form preservation, and data updates according to `PRD.md`; a component library does not implement these product rules.
- Reuse established components and visual conventions. Do not introduce a separate design system for each screen.
- Every data view handles loading, empty results, errors with retry, and success. Every mutation shows pending and result states and preserves entered values on recoverable errors.
- Show real persisted application data; make any demonstration data explicit.
- Verify affected screens at phone and desktop widths, including keyboard focus and the absence of horizontal page overflow.
- Browser inspection is part of verifying implemented UI and does not require a separate request. Inspect affected screens and fix visible defects; do not claim visual verification without viewing the result.

## Git and safety

- Check `git status` before changes and inspect the target remote before remote Git operations. Preserve other people's uncommitted work; do not reset, clean, or reformat it.
- Do not perform destructive Git operations without explicit authorization. A request to review or edit files does not itself authorize publishing them.
- Keep temporary files in the system temporary directory and remove your own files when done. Use available ports and stop only processes you started.
- Never print or commit secrets, tokens, cookies, environment secret values, or real customer data. Provide configuration examples without secrets; never expose server secrets in frontend build variables.
- Never weaken authentication, permissions, validation, or other security controls to make an implementation or check pass.
- Change generated artifacts through their owning source or generator where applicable.

## Documentation

- Keep one authoritative home for each fact: product requirements in `PRD.md`; agreed technical choices, task order, and verified progress in `TASKS.md`; setup and operations in `README.md` or focused technical guides; agent rules in `AGENTS.md`. Link instead of duplicating details.
- Update the owning document when behavior, contracts, or operations change. Avoid documentation that merely repeats code.
- Add a repository map and runnable check commands once the corresponding structure and tooling exist. Keep them tied to actual files and configuration.

<!-- context7 -->
## Current library documentation

Use Context7 MCP to fetch current documentation whenever the user asks about a library, framework, SDK, API, CLI tool, or cloud service, including API syntax, configuration, version migration, library-specific debugging, setup instructions, and CLI usage. Use it even for well-known tools; prefer it over web search for library documentation.

Do not use it for refactoring, writing scripts from scratch, debugging business logic, code review, or general programming concepts unless library-specific documentation is also needed.

1. Start with `resolve-library-id`, providing the library name and what to look up, unless the user provides an exact `/org/project` library ID.
2. Select the best match by exact name, description relevance, snippet count, source reputation, and benchmark score. Retry alternate names or queries if results do not fit. Use version-specific IDs when a version is specified.
3. Call `query-docs` with the selected ID and a descriptive query scoped to one concept. Use separate calls for distinct concepts unless the question concerns their interaction.
4. Base the answer or implementation on the fetched documentation. If Context7 is unavailable, disclose that and use current official documentation as a fallback.
<!-- context7 -->
