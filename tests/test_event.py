from datetime import timedelta

from butterflymx import Call
from homeassistant.const import STATE_UNKNOWN
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.butterflymx.const import SCAN_INTERVAL

ENTITY = "event.unit_101_intercom_call"


def _call(call_id: str, status: str = "INITIALIZING", logged_at: str = "2026-01-05T00:00:00Z") -> Call:
    return Call(
        {
            "id": call_id,
            "loggedAt": logged_at,
            "displayStatus": status,
            "notificationType": "VISITOR",
            "imageUrl": f"https://img.example/{call_id}",
            "device": {"name": "Main Entrance"},
        }
    )


def _record_call_ids(hass) -> list[str]:
    fired: list[str] = []

    @callback
    def record(event) -> None:
        if event.data["entity_id"] == ENTITY:
            fired.append(event.data["new_state"].attributes.get("call_id"))

    hass.bus.async_listen("state_changed", record)
    return fired


async def _poll(hass, mock_client, calls):
    original = mock_client.tenant.get_overview.side_effect

    def with_calls():
        overview = original()
        overview.calls = calls
        return overview

    mock_client.tenant.get_overview.side_effect = with_calls
    async_fire_time_changed(hass, dt_util.utcnow() + SCAN_INTERVAL + timedelta(seconds=1))
    await hass.async_block_till_done()
    mock_client.tenant.get_overview.side_effect = original


async def test_calls_at_startup_do_not_fire(hass, setup_integration):
    state = hass.states.get(ENTITY)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["device_class"] == "doorbell"
    assert state.attributes["event_types"] == ["ring"]


async def test_new_call_fires_ring_with_details(hass, setup_integration, mock_client):
    await _poll(hass, mock_client, [_call("c-2"), *mock_client.tenant.get_overview.side_effect().calls])

    state = hass.states.get(ENTITY)
    assert state.state != STATE_UNKNOWN
    assert state.attributes["event_type"] == "ring"
    assert state.attributes["call_id"] == "c-2"
    assert state.attributes["status"] == "INITIALIZING"
    assert state.attributes["device"] == "Main Entrance"
    assert state.attributes["type"] == "VISITOR"
    assert state.attributes["logged_at"] == "2026-01-05T00:00:00Z"
    assert state.attributes["image_url"] == "https://img.example/c-2"


async def test_same_call_does_not_fire_twice(hass, setup_integration, mock_client):
    events = _record_call_ids(hass)
    await _poll(hass, mock_client, [_call("c-2")])
    # The same call later changes status; that is not a new ring
    await _poll(hass, mock_client, [_call("c-2", status="OPENED_DOOR")])
    await hass.async_block_till_done()
    assert len(events) == 1


async def test_two_calls_between_polls_fire_two_events_oldest_first(hass, setup_integration, mock_client):
    fired = _record_call_ids(hass)
    await _poll(hass, mock_client, [_call("c-4"), _call("c-3")])
    assert fired == ["c-3", "c-4"]


async def test_backlog_is_capped(hass, setup_integration, mock_client):
    fired = _record_call_ids(hass)
    await _poll(hass, mock_client, [_call(f"n-{i}") for i in range(9, 0, -1)])
    assert fired == ["n-5", "n-6", "n-7", "n-8", "n-9"]


async def test_failed_poll_does_not_fire(hass, setup_integration, mock_client):
    from butterflymx import ButterflyMXConnectionError

    mock_client.tenant.get_overview.side_effect = ButterflyMXConnectionError("down")
    async_fire_time_changed(hass, dt_util.utcnow() + SCAN_INTERVAL + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY).state in (STATE_UNKNOWN, "unavailable")
