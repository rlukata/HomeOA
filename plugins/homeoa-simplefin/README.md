# homeoa-simplefin

Automatic bank feed for HomeOA through [SimpleFIN Bridge](https://bridge.simplefin.org)
(features FIN-07 and PLG-03). It pulls posted transactions and balances
into **Money > Bank & deposits**. Syncing never posts to the books: each transaction is
matched or categorized like an uploaded statement. File upload keeps working as a fallback.

## Turn it on

1. Add `simplefin` to `HOMEOA_PLUGINS` (for example `HOMEOA_PLUGINS=simplefin,rulepack_us_wa`)
   and restart. The package ships in the workspace, so `uv sync` installs it.
2. Open **Money > Bank & deposits > Bank feed settings** (`/plugins/simplefin/`).
3. In SimpleFIN Bridge, create a new app connection and copy the **setup token**.
4. Paste it into **Connect**. HomeOA claims it once, stores the resulting access URL
   encrypted (`PluginInstallation.secrets`) and lists the accounts the bridge reports.
5. Link each SimpleFIN account to a HomeOA bank account (or choose **Don't import**).
6. **Sync now** imports right away; after that the worker syncs every 6 hours. Change the
   interval (1 to 24 hours) under **Settings > Plugins > SimpleFIN bank feed**.

Pending transactions are skipped; each sync re-reads the last 7 days, so items that post
later arrive then. Ids are prefixed `simplefin:`, so re-syncing never duplicates.

## Problems

* **Access expired or revoked (403)** and **subscription needs payment (402)** show on the
  bank page and become a board to-do (`homeoa_simplefin.todos.todo_items`). Reconnect with
  a new setup token.
* Network errors are shown and retried at the next sync.
* Messages SimpleFIN passes on from the banks are shown on the bank feed page.

## Fake bridge (tests, demo, E2E)

`homeoa_simplefin.fake_server` is an `httpx.MockTransport` that implements the claim and
`/accounts` protocol with fixed Cedar Row fixtures (operating checking and reserve
savings, four posted transactions, one pending) and a fixed clock of Oct 7 2026,
9:12 am Pacific. It is used when the plugin setting **Use the built-in fake bank** is on
or `HOMEOA_SIMPLEFIN_FAKE=1` (the E2E settings set it). Setup token:
`fake_server.SETUP_TOKEN`. Nothing touches the network.

## Real sandbox check (manual, not in CI)

SimpleFIN offers a demo token on its developer page
(<https://www.simplefin.org/protocol.html>, "Demo"). To try it against a dev server:

1. Run with `HOMEOA_PLUGINS=simplefin` and **without** `HOMEOA_SIMPLEFIN_FAKE`; leave
   **Use the built-in fake bank** off.
2. Connect with the demo setup token; link the demo accounts to throwaway bank accounts.
3. Sync now. Never use a real bank's token on a dev database.

## Job

`homeoa_simplefin.sync_due` is a Procrastinate periodic task (hourly at :07). For each
association with the plugin on and connected, it syncs when `sync_interval_hours` has
passed since the last attempt.
