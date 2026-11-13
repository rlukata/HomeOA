# HomeOA

Open-source (AGPL-3.0) HOA / condo association management platform with a plugin
architecture. Built for small, self-managed associations.

This file is the standing brief for Claude Code, which writes most of the code in this
repository. It holds the rules every change must follow. `README.md` covers setup,
`e2e/README.md` the end-to-end suite, `homeoa/ui/README.md` the UI components and
`deploy/RUNBOOK.md` production deployment.

## Engineering conventions

- **Stack:** Python 3.14 and Django; server-rendered templates with htmx; PostgreSQL only,
  in Docker for development.
- **Tooling:** `uv` for dependencies, `pytest` for tests, `ruff` for lint and formatting.
  `scripts/check.sh` runs lint, format check, i18n extraction and the unit tests; the
  end-to-end suite runs separately. Both must pass before a change lands on `main`.
- **Money:** always `Decimal`, never float. Every financial change posts through the ledger
  service as double entry. HomeOA never initiates outbound payments.
- **Association scope:** every model belongs to an association, even though the UI assumes
  one association.
- **Text:** all user-facing strings go through Django i18n.
- **Plugins:** in-process Python packages discovered via entry points and declared in config.
  Every extension point ships a no-plugin default.
- **Jurisdiction rules:** state-specific rules live in rule packs as data. A value stays
  marked **unverified** until it has been checked against the statute text. Never hardcode
  statute values in core.
- **External services** (Stripe, SimpleFIN, Lob, email): build against the provider
  interfaces with fakes or test doubles. Never use live keys or move real money in
  development or tests. Secrets go in `.env` (gitignored).
- **Comments and tests** explain behaviour in plain words. Name tests after the feature they
  cover.

## Real association data

Records from a real association never enter the repository: not in tests, fixtures,
documentation or logs. Tests and the demo use synthetic data (`seed_demo`, `e2e/fixtures/`).
Real data is loaded once by hand, not through new importer features.

## Decisions

Product, legal, money-handling and scope questions are decided by the maintainer, not by
the agent. When the code or these rules do not settle one, stop and ask, with the options
and a recommendation.
