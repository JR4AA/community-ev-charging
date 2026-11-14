# Payments

One page per payment provider. PayPal is the only one implemented so far;
this directory exists so adding another doesn't mean rewriting the docs.

| Provider | Status | Notes |
|---|---|---|
| [PayPal](paypal.md) | implemented | Checkout, `AUTHORIZE` then partial capture. Card-only guest checkout by default. |
| Stripe | not implemented | Payment Intents with `capture_method=manual` is the same shape and should fit the existing `Payments` interface. |

## What a provider has to be able to do

The charging flow needs **authorise now, capture less later**: hold an amount
while the car charges, then take only the energy actually used and release the
rest. A driver cannot be asked for a final figure up front, because nobody knows
it until they unplug.

That requirement rules out more providers than you would expect:

- **SumUp** online checkout has no pre-authorisation. You would have to charge
  the full hold and refund the difference — and the fee on the refunded part is
  not returned.
- **Zettle** has no server-side payment API at all; it is built around their
  card readers.

So a provider needs: a hosted card page the driver can use without an account,
an authorise-and-later-capture flow with partial capture, a way to void an
authorisation if no energy is taken, and webhooks to confirm what happened.

## Fees

Whatever the provider, the per-charge fixed fee is the thing to design around:
on a small charge it dominates. This project has a `SESSION_FEE` for that, and
leaves the percentage to be covered by the per-kWh markup.

Record the *actual* fee per transaction rather than assuming the published
rate — see the note in [paypal.md](paypal.md) about asking for the full capture
representation, which cost us a day of believing our net income was higher than
it was.
