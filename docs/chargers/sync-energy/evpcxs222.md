# Sync Energy EVPCXS222

A twin-socket 22 kW wall unit, sold in the UK by Sync EV. Observed on firmware
`RD0044-2X11-V1.08`, speaking OCPP 1.6J. Everything below was measured on a
real unit, not taken from a datasheet.

## Identity

```
BootNotification: vendor "Sync Energy", model "EVPCXS222", firmware RD0044-2X11-V1.08
```

**It is one charge point with two connectors, not two charge points.** The OCPP
id is the unit's serial number (e.g. `SP000X000`), and the two sockets are
connectors 1 and 2 of that single id. Connector 0 also reports status, meaning
the charge point as a whole.

Map your socket names onto it with `SOCKETS`:

```
CONNECTORS=CP1,CP2
SOCKETS=CP1=SP000X000:1,CP2=SP000X000:2
```

Getting this wrong is an easy first mistake: if you configure two charge point
ids, the charger connects as one of them and the other is permanently offline.

## The URL

**It appends its own id to whatever URL you configure**, so the configured URL
must end in a slash:

```
wss://ocpp.example.org/ocpp/      ->  connects to /ocpp/SP000X000   correct
wss://ocpp.example.org/ocpp       ->  connects to /ocppSP000X000    404
```

It authenticates with HTTP Basic: the username is the serial number and the
password is a fixed 8-character string set in its own configuration.
`AuthorizationKey` is **not** in its supported key list, so the server cannot
change that password over OCPP — the one-shot provisioning window in this
project will not work with it. Set `OCPP_PASSWORD_<serial>` to the password the
charger already has.

## TLS: this one needs an RSA certificate

**The most important thing on this page.** Its TLS stack predates the extension
mechanism. The ClientHello is 106 bytes on the wire, which leaves room for
nothing beyond the version, random, cipher list and compression byte:

- no SNI
- no `supported_groups`
- no `signature_algorithms`
- no `supported_versions`

Consequences:

- **It cannot use an ECDSA-only endpoint.** Against a Cloudflare free-plan
  tunnel — whose Universal SSL is ECDSA whichever CA you pick — it gets
  `fatal handshake_failure (40)` and gives up. Nothing appears in the
  application log, because TLS dies before any HTTP request exists.
- **It needs an RSA certificate**, and a server that will negotiate without
  `supported_groups`. Let's Encrypt issues RSA with `certbot --key-type rsa`
  (certbot defaults to ECDSA since 2.0), chaining to `ISRG Root X1`.
- Because it sends no SNI, the endpoint must serve a default certificate rather
  than selecting one by name.

See [../../ocpp.md](../../ocpp.md) for the local-TLS-terminator setup.

Observed working: plain `ws://` on the LAN, and `wss://` through a local
terminator holding an RSA certificate. Observed failing: `wss://` to a
Cloudflare free-plan tunnel. For reference, the manufacturer's own cloud
platform terminates TLS on AWS with an RSA certificate, off any CDN.

## Configuration keys

42 keys, of which these are **not supported**:

```
AdditionalRootCertificateCheck   CertificateSignedMaxChainSize
CertificateStoreMaxLength        ConnectorPhaseRotationMaxLength
ConnectorSwitch3to1PhaseSupported  CpoName
CustomDisplayCostAndPrice        DefaultPrice / DefaultPriceText
MeterValuesAlignedDataMaxLength  MeterValuesSampledDataMaxLength
MinimumStatusDuration            NextTimeOffsetTransitionDateTime
ReserveConnectorZeroSupported    SecurityProfile
StopTransactionOnInvalidId       StopTxnAlignedDataMaxLength
StopTxnSampledDataMaxLength      SupportedFeatureProfilesMaxLength
BlinkRepeat                      TimeOffset
```

Worth knowing:

- **`GetConfigurationMaxKeys = 25`** — a bare `GetConfiguration` returns only 25
  of the 42. Ask for the rest by name to see everything.
- **No `CustomDisplayCostAndPrice`**, so the OCA California pricing messages do
  nothing: it will not show prices on its screen.
- **No `SecurityProfile`**, so OCPP security profiles are not selectable.
- **No `MinimumStatusDuration`**, so status dwell times are not adjustable.
- `ConnectionTimeOut = 180` is the *Preparing* window — from plugging in until
  an unstarted transaction is cancelled. It is **not** a Finishing timeout.
  Think twice before lowering it: it is also the time a driver has to scan,
  open the page and complete a card payment.
- `OEM_RSSI` is read-only and reports both radios, e.g.
  `Modem: -71. WiFi: -88 dBm.` — handy for deciding which link it should use.
  Set `SIGNAL_KEY=OEM_RSSI` and both appear in `/api/status` and as Home
  Assistant sensors; see [../../home-assistant.md](../../home-assistant.md).
- `SupportedFeatureProfiles = Core, FirmwareManagement, LocalAuthListManagement,
  Reservation, SmartCharging, RemoteTrigger`.

## Behaviour notes

- **The first `ChangeConfiguration` after boot sometimes times out** after 30 s;
  it succeeds on a later connection. Don't read one timeout as a fault.
- **`Finishing` persists while the cable is in.** After a remote stop the
  connector goes to `Finishing` and stays there; we have only seen it clear when
  the cable was physically removed, so assume it will not free the socket on its
  own. `UnlockConnector` and `ChangeAvailability` are both in its Core profile
  and are the levers worth trying.
- `StopTransactionOnEVSideDisconnect = true` (read-only): unplugging the car
  ends the transaction, which is what this project expects.
- It reconnects by itself within about 20 seconds of the server restarting.

## Physical

22 kW shared between the two sockets, so two cars charging together get about
11 kW each. Untethered (socketed), both connectors on the one unit.
