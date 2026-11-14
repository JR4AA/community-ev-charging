# Charger notes

One page per charger, as `<manufacturer>/<model>.md`, recording what it actually
does rather than what its datasheet says: how it identifies itself, which OCPP
settings it supports, and anything that caught us out.

OCPP 1.6 leaves a lot to the manufacturer. Two chargers that both "support
OCPP 1.6J" can differ in whether one physical unit is one charge point or two,
whether the id goes in the URL path, which configuration keys exist, and — as
the Sync Energy page below shows — whether the TLS stack can talk to a modern
endpoint at all.

| Manufacturer | Model | OCPP | Notes |
|---|---|---|---|
| [Sync Energy](sync-energy/evpcxs222.md) | EVPCXS222 | 1.6J | Twin socket, one charge point. **Needs an RSA certificate** — cannot use a CDN edge that serves ECDSA only. |

If you get another charger working, a page here is the most useful thing you can
contribute. Useful things to record: the vendor and model strings it sends in
BootNotification, whether it is one charge point or several, how it builds the
URL, its full configuration key list, and whether its TLS stack reaches your
endpoint.
