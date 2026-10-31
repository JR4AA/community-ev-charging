# OCPP: the charger side

`evcharge/csms.py` is an OCPP central system (The Mobility House `ocpp`
library), running in the same container as the pay pages. It speaks **1.6J**
(`csms.py`) and **2.0.1** (`csms201.py`) on the same URL: the charger offers a
version in its WebSocket handshake (`ocpp1.6` or `ocpp2.0.1`) and gets the
first one it lists that the server speaks. The admin charger page shows which.

The tables below are in 1.6 terms; [OCPP 2.0.1](#ocpp-201) has the differences.

## A charge

| Driver | Charger → server | Server |
|---|---|---|
| Plugs in | StatusNotification `Preparing` | Pay page offers payment (before that it says "plug your car in first", or offline / in use / out of service) |
| Pays | | Hold placed, then RemoteStartTransaction with the session's idTag (first 20 chars of the session id). If the charger refuses, the hold is voided at once |
| | StartTransaction | Accepted only for that session's tag, on that socket, while it's AUTHORISED; anything else gets `Invalid` and the charger stops (`StopTransactionOnInvalidId`) |
| Waits | MeterValues every 60 s | Readings stored. When cost + session fee comes within `STOP_MARGIN` (50p) of the hold: RemoteStopTransaction |
| Unplugs / presses Stop on the status page | StopTransaction (with any queued readings) | Settled: each kWh priced by the charger's timestamps, captured, rest released. A failed capture is retried every minute |
| Unplugs before charging starts | StatusNotification `Available` | Hold voided |

On connect the server sets `MeterValueSampleInterval=60`,
`MeterValuesSampledData=Energy.Active.Import.Register`,
`StopTransactionOnInvalidId=true`, and asks for each connector's status.

Handlers must never `await` a call to the same charger: the library reads the
charger's reply only after the handler returns, so it deadlocks for 30 s.
Spawn a task instead (see `check_limit`).

## Connecting the charger

Settings in `/etc/evcharge/env`:

```
# SOCKETS=CP1=CP1:1,CP2=CP2:1     # socket name = charge point id : connector (the default)
# If the twin unit is ONE charge point with two connectors, e.g.:
# SOCKETS=CP1=<cp id>:1,CP2=<cp id>:2
OCPP_PASSWORD_CP1=<long random>            # required via the tunnel
OCPP_PASSWORD_CP2=<long random>
# OCPP_ALLOW_WS_CP1=true                   # only for a charger that can't do wss://
SESSION_FEE=0.30
```

In the charger's settings (its app, web UI, or current operator's portal):

| | **Default: wss** (4G, Wi-Fi or LAN) | Fallback: plain ws (LAN only) |
|---|---|---|
| Central system URL | `wss://ev.example.org/ocpp/` | `ws://<OCPP_BIND address>:9000/` |
| Charge point id | `CP1` (appended to the URL) | same |
| Password / AuthorizationKey | `OCPP_PASSWORD_CP1`, required | `OCPP_PASSWORD_CP1` if set |
| When | always, unless the charger can't do TLS | only with `OCPP_ALLOW_WS_CP1=true`, from a private address |

The wss route goes through the Cloudflare tunnel even from your own LAN; it's
the one with a certificate the charger can verify.

Port 9000 is published on the address in `OCPP_BIND`, set in `.env` beside `compose.yml`; without it, localhost only.

## Who may connect (enforced in the app)

- Only charge point ids in `SOCKETS`; anything else gets 404.
- Only the `ocpp1.6` or `ocpp2.0.1` subprotocol (400 otherwise).
- Encrypted: through the tunnel the charger must have used `wss://` (Cloudflare's
  `Cf-Visitor` says so; 403 otherwise). A direct, unencrypted connection is only
  accepted from an RFC 1918 or loopback address **and** for a charge point with
  `OCPP_ALLOW_WS_<id>=true` (403 otherwise).
- If a password is set for that charge point: HTTP Basic, user = charge point id
  (401 otherwise, compared in constant time).
- A connection that came **through the tunnel** (it carries `Cf-Connecting-Ip`)
  is refused outright (403) when no password is set for that charge point.
- 5 failed passwords from one address in 15 minutes locks it out for 15 minutes (429).

**The tunnel example has the `/ocpp/` rule commented out.** To open the wss
route, set each charger's password, uncomment the rule in
`/etc/cloudflared/config.yml`, and restart cloudflared. `check-public.sh` checks
that a password-less handshake there gets the app's 401.

## Testing without the charger

```
docker compose exec app python -m evcharge.simulator ws://127.0.0.1:9000/CP1 --kw 7 --speed 60
```

The simulator boots, plugs a car in, accepts the remote start that payment
sends, and charges until stopped (Stop button, the hold limit, or
`--unplug-after-kwh`). `--speed 60` runs a minute of charging per second.
`tests/test_ocpp.py` drives the same simulator over a real websocket.

## OCPP 2.0.1

The same flow in 2.0.1's messages:

| 1.6 | 2.0.1 |
|---|---|
| StatusNotification `Preparing` | StatusNotification `Occupied` (shown as Preparing while nothing is paid on that EVSE) |
| RemoteStartTransaction / RemoteStopTransaction | RequestStartTransaction (idToken type `Central`, a `remoteStartId`) / RequestStopTransaction |
| StartTransaction, MeterValues, StopTransaction | TransactionEvent `Started` / `Updated` / `Ended` |
| Connector id | EVSE id (the number in `SOCKETS`, e.g. `CP1=MYCP:1`) |
| ChangeConfiguration / GetConfiguration | SetVariables / GetBaseReport (answered in NotifyReport messages) |

On connect the server sets `SampledDataCtrlr` `TxUpdatedInterval=60`, and
`TxUpdatedMeasurands`, `TxStartedMeasurands`, `TxEndedMeasurands` to
`Energy.Active.Import.Register`, and `TxCtrlr.StopTxOnInvalidId=true`.

The charger numbers its own transactions (strings, kept in
`transactions.ocpp_tx_id`) and may start one as soon as the cable goes in
(`TxStartPoint`), before anyone pays. The server follows such a transaction in
memory only. When the payment's idToken (or `remoteStartId`) arrives on it, it
gets a row in `transactions` starting from the meter reading at that moment, so
energy before payment is never billed. A transaction with an idToken we don't
recognise gets `Invalid`, and the charger stops it.

Settings appear on the diagnostics page as `Component.Variable`, e.g.
`SampledDataCtrlr.TxUpdatedInterval` or `TariffCostCtrlr.Enabled[Cost]`.

**Prices on the screen** use the standard `TariffCostCtrlr`, if the charger
reports it (and `DISPLAY_PRICES` isn't `off`): the server enables `Tariff` and
`Cost`, sets `TariffFallbackMessage` to the tariff text and `Currency`, and
puts `totalCost` plus a line of text (`updatedPersonalMessage`, e.g. "3.20 kWh,
£1.17 so far. Now 27p/kWh.") in its answer to every TransactionEvent. The
`Ended` answer carries the final total.

Test with `python -m evcharge.simulator … --ocpp 2.0.1`.

## Charger diagnostics

`/admin/chargers/<id>` (linked from the sockets on `/admin`): online or not,
last heard from, make, model, firmware and serial from its BootNotification,
each connector's state, the OCPP version, whether prices go to its screen, and every setting the
charger reports (`GetConfiguration`, or `GetBaseReport` in 2.0.1, read on connect and on demand with
"Read settings from the charger"). That's where network type, signal strength
and any vendor keys show up.

## Prices on the charger's screen

(1.6; for 2.0.1 see [above](#ocpp-201).) OCPP 1.6 has no display message, but the Open Charge Alliance's "California
pricing" extension (whitepaper v3.1) does it with `DataTransfer`, vendor
`org.openchargealliance.costmsg`. With `DISPLAY_PRICES=auto` (the default) the
server uses it only when the charger reports `CustomDisplayCostAndPrice`:

- on connect: sets `CustomDisplayCostAndPrice=true` and `DefaultPrice` to the
  tariff text, e.g. "Off-peak 27p/kWh, Peak 46p/kWh 16:00-19:00, +30p per charge";
- `RunningCost` straight after the charge starts and after every meter reading:
  cost so far, current price per kWh, the session fee, and the next price change
  (with a meter-value trigger at that time);
- `FinalCost` when it stops: the total and, with `PUBLIC_URL`, the session link
  as a QR code. Free (admin) charges show no cost.

`DISPLAY_PRICES=off` never sends them. Messages are spawned, not awaited, from
inside the charger's handlers (see the deadlock note above).
