# HomeOA end-to-end suite

pytest + Playwright (sync API).

## Run

```sh
docker compose up -d db                    # from repo root
uvx --from playwright playwright install chromium   # once
uv run pytest e2e -p no:django             # all scenarios
uv run pytest e2e -p no:django --headed --slowmo 300   # watch it
```

Needs `pytest-playwright` and `pyotp` (in the dev dependency group).

## Design

- **Server:** `conftest.live_server_url` (session) runs `migrate` then `manage.py runserver` on a free port with `DJANGO_SETTINGS_MODULE=homeoa.settings.e2e` (override with `HOMEOA_E2E_SETTINGS`) against a separate DB (`homeoa_e2e`). Waits on `/healthz`. Set `HOMEOA_E2E_BASE_URL` to test an already running server instead.
- **Reset:** the `seed` fixture runs `manage.py seed_demo --reset` before each test. Tests never share state.
- **Mail:** `mail` fixture. The server uses Django's filebased backend writing to `HOMEOA_E2E_MAIL_DIR` (default `.work/e2e-mail`), and the fixture reads it. Locmem is not usable because the server runs in another process.
- **Login:** `login(email, page=None)` uses the test-only `e2e_login_url` command. Only `test_magic_link_login` goes through the inbox.
- **Phone:** `phone_page` is a 390x844 mobile context.
- **a11y:** `axe(page)` injects axe-core 4.10.2 and fails on serious/critical violations. Offline: set `HOMEOA_E2E_AXE_JS` to a local axe.min.js. The page-scan test runs axe on every path in `PAGES`; add a path to cover a new page. `HOMEOA_E2E_AXE_LOG=1` (with `-s`) prints all violations, including minor/moderate.
- **CSP:** the e2e settings enforce the Content-Security-Policy, and a conftest guard fails any test that triggers a violation.
- **Locators:** role/label/text from the prototype labels; `data-testid` only for values with no accessible name.
- **External services:** fakes only (SimpleFIN fake bridge, fake Stripe, fake Lob). No live keys.

## Test hooks in the app

The suite drives the server only through `manage.py`. These commands exist for it and
refuse to run outside e2e/test settings:

| Command | Purpose |
|---|---|
| `seed_demo --reset` | Rebuild the demo association (sample units A–I, board, owners, tenants) |
| `e2e_login_url <email>` | Print a single-use magic-link path via the real token service |
| `e2e_age_magic_links --minutes N` | Backdate unused magic links (expiry checks) |
| `e2e_residents on\|off` | Turn the resident-access switch on or off |
| `module on\|off <module_id> [--association <id or short name>] [--all]`, `module list` | Switch an optional module per association, or list the switches (also allowed under dev settings) |
| `e2e_bounce <email>` | Mark the latest message to that address bounced via the real bounce handler |
| `stripe_fake …` | Drive the fake Stripe provider (last intent, fail next, signed webhooks) |
| `run_scheduled --as-of YYYY-MM-DD` | Run due periodic jobs synchronously for a date |

## Files

- `conftest.py`: fixtures and the `manage()` helper
- `helpers/`: mail, auth, a11y and seed helpers
- `fixtures/`: bank CSV; `fixtures/appfolio/`: synthetic AppFolio-style CSVs for the importer
