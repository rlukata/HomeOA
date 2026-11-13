# Contributing to HomeOA

Bug reports, questions and pull requests are welcome.

## Before you start

- **Bugs and small fixes:** open an issue or go straight to a pull request.
- **Features and larger changes:** open an issue first and describe what you want to
  change. HomeOA handles an association's money and legal notices, so product, legal and
  money-handling choices are agreed before code is written.
- **Security problems:** do not open a public issue. See [SECURITY.md](SECURITY.md).

## Setting up

[`README.md`](README.md) has the development setup. In short: `uv`, Docker for PostgreSQL,
then `uv run manage.py migrate` and `uv run manage.py seed_demo`.

## Rules every change follows

[`CLAUDE.md`](CLAUDE.md) holds the engineering conventions. They apply to code written by
people and by coding agents alike. The ones that most often matter:

- Money is always `Decimal`, and every financial change posts through the ledger service
  as balanced double entry.
- HomeOA never initiates outbound payments.
- Every model belongs to an association.
- All user-facing text goes through Django i18n.
- State-specific rules are data in a rule pack, never constants in core code.
- External services are reached through the provider interfaces. Tests use fakes; never
  live keys or real money.
- **No real association data** anywhere in the repository: not in tests, fixtures,
  screenshots, issues or logs. Use the demo association or invent data.

## Checks

Run these before opening a pull request. CI runs the same checks.

```sh
scripts/check.sh                       # lint, format, i18n extraction, unit tests
uv run pytest e2e -p no:django         # end-to-end suite (see e2e/README.md)
```

The full suites are slow. While working, run the tests for the app you changed, for
example `uv run pytest homeoa/ledger`, and let CI run the rest.

A change in behaviour needs a test. Name tests after the feature they cover, and add an
end-to-end test when the change alters what a board member or owner sees.

## Pull requests

- Keep each pull request to one change, with a description of what it does and why.
- Include a screenshot for a visible UI change.
- Pull requests are squash-merged, so the title becomes the commit message.

## Licence

HomeOA is licensed under the [GNU AGPL v3.0 or later](LICENSE). By contributing you agree
that your contribution is licensed under the same terms.
