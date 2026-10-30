# PayPal

## How the money moves

1. The driver picks a hold amount and pays on `/pay/<socket>`. The service
   creates a PayPal order with intent `AUTHORIZE` for that amount, and once the
   driver approves it, authorises it: the money is **held, not taken**.
2. When charging stops, the service captures the cost with `final_capture`, so
   PayPal releases the rest of the hold at once. The cost is each kWh at the
   band in force when it was delivered (from the charger's timestamped meter
   readings), plus `SESSION_FEE`, rounded to the penny and never more than the hold.
3. If no energy was delivered, or nobody starts charging within
   `UNUSED_AUTH_MINUTES`, the hold is voided and the driver pays nothing.

The tariff, fee and hold are fixed when the session is created, so a price
change never affects anyone mid-charge.

Every PayPal POST carries a `PayPal-Request-Id` derived from the session, so a
retry after a timeout or a 5xx returns the original result instead of charging
twice. A 5xx leaves the session retryable (retried every minute); a 4xx marks it
`FAILED`, and `python -m evcharge.cli show <session>` shows why.

Webhooks (`/webhooks/paypal`) are verified with PayPal's
verify-webhook-signature API before anything is acted on. They catch captures
that complete later, holds voided from the PayPal dashboard, and a hold PayPal
clears after the service had already refused it (which is then voided).
Refunds and reversals are logged only.

No personal data is stored: no names, no emails, nothing about the payer.

## Fees

UK rates at the time of writing (check PayPal's business fees page):

| | Fee |
|---|---|
| Card, no PayPal account (guest checkout; `PAYMENT_METHODS=card`) | 1.2% + 30p |
| PayPal account (`PAYMENT_METHODS=all`) | 2.9% + 30p |
| Charity rate (on application; ask whether it covers sales) | 1.4% + 20p |

A `SESSION_FEE` equal to the fixed fee, and rounding prices up to the next
penny, keeps every charge above cost.

## Setting up

1. At <https://developer.paypal.com>, signed in with your PayPal **business**
   account, create a **Sandbox** app under Apps & Credentials (type Merchant;
   the name can't contain "paypal"). Its client ID and secret go in
   `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET`.
2. On the app, add a webhook: URL `https://<your host>/webhooks/paypal`, events
   `PAYMENT.AUTHORIZATION.CREATED`, `PAYMENT.AUTHORIZATION.VOIDED`,
   `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.PENDING`,
   `PAYMENT.CAPTURE.DENIED`, `PAYMENT.CAPTURE.REFUNDED`,
   `PAYMENT.CAPTURE.REVERSED`. Its ID goes in `PAYPAL_WEBHOOK_ID`.
3. Test with the simulator ([ocpp.md](ocpp.md)) and a sandbox card from Testing
   Tools → Card Testing. Check the sandbox business account: one payment for
   the captured amount, nothing left held.
4. To go live, create a **Live** app with its own credentials and webhook, set
   `PAYPAL_ENV=live`, and restart. Card-only checkout needs "PayPal account
   optional" turned on in the business account's website payment settings.
