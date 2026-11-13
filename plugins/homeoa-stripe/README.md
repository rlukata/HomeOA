# homeoa-stripe

ACH (US bank account) payments through Stripe as a `PaymentProvider` (FIN-06, POR-02).

- Inbound only: owners pay the association. HomeOA never initiates payouts.
- Clients: `HOMEOA_STRIPE_CLIENT=live` (default, forced in production; test-mode `sk_test_`
  keys only for now) or `fake` (dev/test/E2E, in-repo `homeoa_stripe.fake`, no network).

## Bank accounts and mandates

Owners link a bank account at `/plugins/stripe/bank/?unit=<n>` (`bank.py`, `views.py`):

1. `bank.start` creates (or reuses) a Stripe customer and a SetupIntent with
   `verification_method=automatic`: Financial Connections (instant) first, microdeposits
   when the bank isn't supported or numbers are typed by hand.
2. Stripe.js collects the account in the browser (live); in fake mode buttons stand in
   (`instant`, `manual`, `fail`).
3. The owner ticks the authorization text; `bank.confirm` confirms with an **online**
   acceptance (IP and browser) and stores a `StripeMandate`. There is no offline path.
4. Microdeposits: `bank.verify` checks the amounts (test: 32 and 45) or the descriptor code
   (`SM11AA`). Only a verified mandate is `chargeable`.
5. Removing an account revokes the mandate and turns off autopay that used it.

Every debit (`services.create_payment`) needs a chargeable mandate for that unit and
takes an idempotency key, so a repeated submit returns the first intent.

## Pay now and autopay

The core Pay tab (`homeoa/portal/pay.py`) and autopay (`homeoa/payments/autopay.py`) use
the provider methods `saved_methods`, `setup_url`, `pending`, `collect` and `fee_quote`.
Money is recorded only when the webhook reports success. Failed autopay debits reach
`payments.autopay.reference_failed` from the webhook.

## Webhooks and ledger

- `POST /hooks/stripe/<association_id>/` with a `Stripe-Signature` header
  (`t=<ts>,v1=<hmac-sha256 of "<ts>.<body>">`). `payment_intent.succeeded` records a receipt
  through `payments.services.record_receipt` once per intent, in the ownership period the
  intent was created for.
- Payments land in 1060 "Stripe clearing"; absorbed fees post Dr 5450 "Bank & payment
  processing fees" / Cr 1060, so 1060 equals the net payout Stripe owes.

## Fees

Fee pass-through needs both the rule `payments.fee_pass_through_allowed` (the WA pack
allows it, attorney review pending) and the board opt-in `fee_mode="pass_through"`
(settable only via `save_installation`). Otherwise the association absorbs the fee. When a
fee is passed through, the Pay page shows it with the no-fee alternative (check, or a
transfer the owner sends) whenever `payments.free_method_required` is on.

## Testing

- `manage.py stripe_fake last-intent | fail-next | webhook <event_type> <intent_id>` drives
  the fake for E2E. The demo seed links verified fake bank accounts for the owners
  of units A and C, which the E2E pay flows rely on.
- Opt-in live test against Stripe test mode (never in CI):
  `HOMEOA_ENV_FILE=/path/to/.env uv run pytest -m live_stripe`. It links a test bank
  account through `bank.py`, debits unit C's dues, and, when the Stripe CLI is installed,
  receives the real webhook through `stripe listen` (key passed via the `STRIPE_API_KEY`
  environment variable, never on the command line).
