"""The Home Assistant integration against a real HA (pytest-homeassistant-custom-component).

Run: .venv-ha/bin/python -m pytest tests_ha -o asyncio_mode=auto
"""
from homeassistant import config_entries
from homeassistant.const import CONF_URL
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.community_ev_charging.const import CONF_TOKEN, DOMAIN

URL = "http://192.168.1.20:8081"
STATUS = {
    "currency": "GBP",
    "sockets": {
        "CP1": {"state": "Charging", "online": True, "charge_point": "CP1", "power_kw": 7.2,
                "session": {"id": "s1", "status": "charging", "kind": "paid", "energy_kwh": 4.21, "cost": 1.44,
                            "hold": 40.0, "limit_kwh": None, "started_at": "2026-11-02T15:04:11+00:00", "note": None}},
        "CP2": {"state": "Offline", "online": False, "charge_point": "CP2", "power_kw": 0.0, "session": None},
    },
    "chargers": {"CP1": {"online": True, "vendor": "Sync", "model": "Pro", "firmware": "1.2.3", "serial": "SN1",
                         "last_seen": "2026-11-02T15:30:00+00:00"},
                 "CP2": {"online": False}},
    "solar_rate_on": False,
    "today": {"kwh": 4.21, "taken": 0.0, "charges": 1},
    "month": {"kwh": 52.3, "taken": 14.92, "paypal_fees": 1.42, "free_kwh": 2.0, "charges": 6},
}


async def test_config_flow_checks_the_server(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/status", status=401)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL + "/", CONF_TOKEN: "bad"})
    assert result["errors"] == {"base": "invalid_auth"}

    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/status", json=STATUS)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL, CONF_TOKEN: "good"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_URL: URL, CONF_TOKEN: "good"}
    assert aioclient_mock.mock_calls[-1][3]["Authorization"] == "Bearer good"


async def test_config_flow_cannot_connect(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/status", exc=TimeoutError())
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_URL: URL, CONF_TOKEN: "x"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_entities(hass, aioclient_mock):
    aioclient_mock.get(f"{URL}/api/status", json=STATUS)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL, data={CONF_URL: URL, CONF_TOKEN: "good"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    def state(entity_id):
        s = hass.states.get(entity_id)
        assert s is not None, f"{entity_id} missing; have {sorted(hass.states.async_entity_ids())}"
        return s

    assert state("sensor.socket_cp1_state").state == "charging"
    assert state("sensor.socket_cp1_power").state == "7.2"
    assert state("sensor.socket_cp1_power").attributes["unit_of_measurement"] == "kW"
    assert state("sensor.socket_cp1_session_energy").state == "4.21"
    assert state("sensor.socket_cp1_session_cost").attributes["unit_of_measurement"] == "GBP"
    assert state("binary_sensor.socket_cp1_online").state == "on"
    assert state("binary_sensor.socket_cp1_charging").state == "on"
    assert state("binary_sensor.socket_cp1_charging").attributes["kind"] == "paid"
    assert state("sensor.socket_cp2_state").state == "offline"
    assert state("binary_sensor.socket_cp2_online").state == "off"
    assert state("sensor.ev_charging_energy_this_month").state == "52.3"
    assert state("sensor.ev_charging_taken_this_month").state == "14.92"
    assert state("binary_sensor.ev_charging_solar_rate").state == "off"

    from homeassistant.helpers import device_registry as dr
    dev = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"{entry.entry_id}_CP1"), entry.entry_id)
    assert (dev.manufacturer, dev.model, dev.sw_version, dev.serial_number) == ("Sync", "Pro", "1.2.3", "SN1")

    # Token revoked on the server: HA starts a re-authentication flow.
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{URL}/api/status", status=401)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert any(f["context"]["source"] == "reauth" for f in hass.config_entries.flow.async_progress())
    assert await hass.config_entries.async_unload(entry.entry_id)
