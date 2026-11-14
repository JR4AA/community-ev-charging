# Home Assistant

`GET http://<OCPP_BIND>:8081/api/status` returns the chargers' state as JSON, for
HA's `rest` integration. Church network only (the tunnel never forwards `/api`,
and the app refuses anything from it or from a public address), and a Bearer
token: `API_TOKEN` in `/etc/evcharge/env`, separate from the admin password.

```json
{
  "sockets": {
    "CP1": {"state": "Charging", "online": true, "power_kw": 7.2,
               "session": {"id": "…", "status": "charging", "kind": "paid", "energy_kwh": 4.21,
                           "cost": 1.44, "hold": 40.0, "limit_kwh": null,
                           "started_at": "2026-11-02T15:04:11+00:00", "note": null}},
    "CP2": {"state": "Available", "online": true, "power_kw": 0.0, "session": null}
  },
  "chargers": {"CP1": {"online": true, "vendor": "…", "model": "…", "firmware": "…",
                          "serial": "…", "last_seen": "2026-11-02T15:30:00+00:00"}},
  "solar_rate_on": false,
  "today": {"kwh": 4.21, "taken": 0.0, "charges": 1},
  "month": {"kwh": 52.3, "taken": 14.92, "paypal_fees": 1.42, "free_kwh": 2.0, "charges": 6}
}
```

`state` is the OCPP connector status (`Available`, `Preparing` = car plugged in,
`Charging`, `SuspendedEV`, `Finishing`, `Faulted`, `Unavailable`), or `Offline`.
`power_kw` is worked out from the last two meter readings (one a minute).

## The integration (recommended)

`custom_components/community_ev_charging` in this repo is a Home Assistant
integration for this endpoint: one device per socket (state, power, session
energy and cost, online, charging) and a site device (energy today and this
month, money taken, PayPal fees, charges and the solar-rate switch), plus - when
configured - certificate and radio-signal sensors. It polls every 30 s.

1. HACS → Integrations → ⋮ → Custom repositories: add
   `https://github.com/jr4aa/community-ev-charging`, category Integration, and
   install "Community EV Charging". (Or copy the folder into `config/custom_components/`.)
2. Restart HA, then Settings → Devices & services → Add integration →
   "Community EV Charging".
3. Server address `http://<OCPP_BIND>:8081`, and the `API_TOKEN`. It checks both
   before saving; if the token is changed later, HA asks for the new one.

Tested with `pytest-homeassistant-custom-component` against HA 2026.10:
`uv venv --python 3.14 .venv-ha && uv pip install --python .venv-ha/bin/python
pytest-homeassistant-custom-component`, then
`.venv-ha/bin/python -m pytest tests_ha -o asyncio_mode=auto`.

## Solar rate from Home Assistant

The integration's **Solar rate** switch (`switch.ev_charging_solar_rate`) tells
the server there's spare solar (`POST /api/solar {"on": true}`, same token).
While it's on, energy is billed at `SOLAR_PRICE_PER_KWH`, split by time like the
other bands; with that setting commented out the switch has no effect on prices.
It changes the price only, not the charger's power.

The rule (here: exporting at least 19 kW with the battery full) lives in HA. A
template binary sensor holds it, with a lower threshold to turn off than to turn
on and a 5-minute delay each way, so a passing cloud doesn't flip the price; an
automation copies it to the switch. Replace the two source sensors with yours
(export power in W here, battery state of charge in %):

```yaml
template:
  - binary_sensor:
      - name: "Spare solar"
        unique_id: spare_solar
        delay_on: "00:05:00"
        delay_off: "00:05:00"
        state: >-
          {% set export_kw = states('sensor.grid_export_power') | float(0) / 1000 %}
          {% set soc = states('sensor.battery_soc') | float(0) %}
          {% if is_state('binary_sensor.spare_solar', 'on') %}
            {{ export_kw >= 15 and soc >= 95 }}
          {% else %}
            {{ export_kw >= 19 and soc >= 98 }}
          {% endif %}

automation:
  - alias: "EV solar rate follows spare solar"
    triggers:
      - trigger: state
        entity_id: binary_sensor.spare_solar
        to: ["on", "off"]
      - trigger: homeassistant
        event: start
    actions:
      - action: >-
          {{ 'switch.turn_on' if is_state('binary_sensor.spare_solar', 'on') else 'switch.turn_off' }}
        target:
          entity_id: switch.ev_charging_solar_rate
```

The start trigger puts the switch right after an HA restart. If HA is down, the
server keeps whatever it was last told.

## Radio signal strength

Some chargers report how good their mobile or Wi-Fi signal is, in a
vendor-specific configuration key. There is no OCPP key for it and no agreed
format, so you name the key and the value is parsed for `label: number` pairs:

```
SIGNAL_KEY=OEM_RSSI        # the Sync Energy EVPCXS222's key; unset = off
SIGNAL_INTERVAL=300        # optional, seconds; re-read no more often than this
```

On the EVPCXS222 that key reads `Modem: -71. WiFi: -88 dBm.`, which becomes:

```json
"chargers": {"<id>": {"signal": {"modem": -71, "wifi": -88}, ...}}
```

It is read when the charger connects and refreshed on its heartbeats, rate
limited to `SIGNAL_INTERVAL` so a charger with a short heartbeat is not asked
every beat. A charger that does not answer keeps its previous reading.

In Home Assistant each radio becomes a sensor on the charge point's first
socket device - `sensor.socket_<name>_modem_signal`,
`sensor.socket_<name>_wi_fi_signal` - with device class `signal_strength` in
dBm, so they chart and can be alerted on like any other signal.

This is worth having where the charger is at the edge of the Wi-Fi: a link that
is merely weak rather than dead shows up as dropped sessions and repeated DHCP,
and the numbers tell you whether the mobile radio would do better.

## Watching the charger's TLS certificate

The charger reaches the server over `wss://`, so if that certificate lapses the
charger silently stops connecting. Two failures are possible and only one is
visible on disk: the certificate not renewing, and it renewing while the TLS
terminator carries on serving the old one. So the check connects to the
endpoint and reads the certificate actually served.

Set in the server's env:

```
TLS_CHECK=ocpp.example.org          # the name on the certificate
TLS_CHECK_ADDR=192.168.0.10          # optional: where to dial, if that name
                                    # only resolves on the site's own DNS
TLS_CHECK_PORT=443                  # optional, default 443
```

`/api/status` then carries:

```json
"tls": {"ok": true, "name": "ocpp.example.org", "expires": "2027-02-12T09:05:55+00:00",
        "days_left": 61, "issuer": "Let's Encrypt"}
```

`ok` is false — with an `error` — if the certificate has expired, is untrusted,
is for the wrong name, or the endpoint is down. Leave `TLS_CHECK` unset and
`tls` is `null` and no entities appear. The result is cached for an hour
(five minutes after a failure), so a 30-second poll doesn't mean a handshake
every 30 seconds.

In the integration this is `sensor.ev_charging_certificate_days_left` and
`binary_sensor.ev_charging_certificate_problem`. A useful alert:

```yaml
automation:
  - alias: "EV charger certificate needs attention"
    trigger:
      - platform: state
        entity_id: binary_sensor.ev_charging_certificate_problem
        to: "on"
        for: "01:00:00"
      - platform: numeric_state
        entity_id: sensor.ev_charging_certificate_days_left
        below: 21
    action:
      - service: notify.persistent_notification
        data:
          title: "EV charger certificate"
          message: >
            {{ state_attr('binary_sensor.ev_charging_certificate_problem', 'error')
               or states('sensor.ev_charging_certificate_days_left') ~ ' days left' }}
```

Renewal itself is certbot's job (`certbot.timer`), with a deploy hook to reload
the terminator; this only tells you when that has stopped working.

## Or plain YAML (no integration)

In `secrets.yaml` (the token from the env file, after the word Bearer):

```yaml
ev_charging_api: "Bearer <API_TOKEN>"
```

In `configuration.yaml`:

```yaml
rest:
  - resource: http://<OCPP_BIND>:8081/api/status
    scan_interval: 30
    headers:
      Authorization: !secret ev_charging_api
    binary_sensor:
      - name: "EV CP1 online"
        value_template: "{{ value_json.sockets.CP1.online }}"
        device_class: connectivity
      - name: "EV CP2 online"
        value_template: "{{ value_json.sockets.CP2.online }}"
        device_class: connectivity
    sensor:
      - name: "EV CP1 state"
        value_template: "{{ value_json.sockets.CP1.state }}"
      - name: "EV CP1 power"
        value_template: "{{ value_json.sockets.CP1.power_kw }}"
        unit_of_measurement: kW
        device_class: power
      - name: "EV CP1 session energy"
        value_template: >-
          {{ value_json.sockets.CP1.session.energy_kwh if value_json.sockets.CP1.session else 0 }}
        unit_of_measurement: kWh
        device_class: energy
      - name: "EV CP2 state"
        value_template: "{{ value_json.sockets.CP2.state }}"
      - name: "EV CP2 power"
        value_template: "{{ value_json.sockets.CP2.power_kw }}"
        unit_of_measurement: kW
        device_class: power
      - name: "EV CP2 session energy"
        value_template: >-
          {{ value_json.sockets.CP2.session.energy_kwh if value_json.sockets.CP2.session else 0 }}
        unit_of_measurement: kWh
        device_class: energy
      - name: "EV energy this month"
        value_template: "{{ value_json.month.kwh }}"
        unit_of_measurement: kWh
        device_class: energy
        state_class: total
      - name: "EV taken this month"
        value_template: "{{ value_json.month.taken }}"
        unit_of_measurement: GBP
        device_class: monetary
```

If HA has an integration for the charger's previous operator, remove it once the
charger has moved. Don't add HA's own OCPP integration as the charger's server; a charger talks to
one central system only.
