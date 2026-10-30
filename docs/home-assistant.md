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
month, money taken, PayPal fees, charges, solar rate). It polls every 30 s.

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
