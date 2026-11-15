> [!WARNING]
> **Very early testing.** This is running at one site, in its first weeks, and
> hasn't yet been proven with a range of chargers or with much real money. Expect
> rough edges and breaking changes. Test it in the PayPal sandbox with the charger
> simulator before taking a single payment, and please report what you find.

# community-ev-charging

Pay-as-you-go car charging for a church, village hall or any community car park
with an OCPP charger, without a charge point operator taking a subscription or
a cut.

Drivers scan a QR code on the socket, pay by card on a web page, and charge.
PayPal holds an amount while they charge, then takes only the cost of the
energy they used and releases the rest.

- **OCPP central system** for the charger, 1.6J or 2.0.1, whichever it asks for
  (built on The Mobility House [`ocpp`](https://github.com/mobilityhouse/ocpp)
  library): starts the charger after payment, reads the meter every minute, stops it before the hold runs out.
- **PayPal Checkout with authorise-then-capture**: card-only guest checkout by
  default, so drivers need no account and the fee is the low guest-card rate.
- **Time-of-use pricing**: peak and off-peak bands in local time, each kWh priced
  by when it was delivered, plus an optional per-charge fee and an optional
  solar-surplus rate.
- **Almost no personal data**: no names and no card details - PayPal handles all
  of that. Turn receipts on and the payer's email is held from payment until the
  charge settles, used for that one message, then cleared; leave them off and no
  address is ever read.
- **Configurable branding**: name, logo, colour, footer.
- **A receipt the driver actually gets** (optional): PayPal tells them about the
  hold and never about the final amount, and a guest has no account to look in.
- **Admin page on your own network**: live sessions, stop a charge, authorise a
  free charge by kWh or £; every charge with monthly totals and a CSV.
- **Home Assistant integration** (installable with HACS): a device per socket with
  state, power, session energy and cost, plus site totals. See
  [docs/home-assistant.md](docs/home-assistant.md).
- One Docker Compose stack; a Cloudflare Tunnel publishes only the payment pages
  and the PayPal webhook.
- **Notes per charger model** in [docs/chargers/](docs/chargers/). OCPP 1.6 leaves
  a lot to the manufacturer — including, on at least one unit, a TLS stack too old
  to reach a CDN edge at all.

```
charger ──OCPP 1.6J / 2.0.1──► app :9000 (LAN, or wss via the tunnel with a password)
                               app :8080 ◄── cloudflared ◄── drivers' phones, PayPal webhooks
                                             only /, /pay, /static, /webhooks/paypal
```

## How a charge works

| Driver | System |
|---|---|
| Plugs in, scans the QR code | Page shows the prices, and offers payment once the socket reports a car connected |
| Picks a hold (e.g. £40) and pays by card | PayPal holds it; the charger is told to start |
| Charges | Meter readings every minute; the status page shows energy and cost so far, with a Stop button |
| Unplugs, presses Stop, or reaches the hold | The cost (each kWh at its band, plus the fee) is captured; the rest of the hold is released |

If the car never charges, the hold is released and nothing is taken.

## Getting started

```
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The tests run the real app and OCPP server against a fake PayPal and a simulated
charger over a real websocket.

To deploy: [docs/deploy.md](docs/deploy.md). Settings: [env.example](env.example).
PayPal setup: [docs/payments/paypal.md](docs/payments/paypal.md). The charger side:
[docs/ocpp.md](docs/ocpp.md).

## Status

Running at one site with a real charger and live PayPal. First real card charge
14 November 2026: authorise, meter, stop, partial capture, all as intended.

That is still one site, one charger model and very little money, so treat it as
early. Test with yours, and please report what you find — especially a page for
[docs/chargers/](docs/chargers/) if you get another model working.

Things to check for your own site before charging the public: VAT on resold
electricity, any reduced-rate VAT or levy relief on your supply, and the Public
Charge Point Regulations 2023 if they apply to you.

## Licence

MIT. See [LICENSE](LICENSE).
