"""Event entity that fires once for each new ButterflyMX intercom call.

ButterflyMX has no feed of new calls for a resident account: its only GraphQL
subscription (callStatusUpdates) needs the ID of a call that is still active,
which the official apps get from a push notification. New calls arrive two
ways: instantly from the tenant webhook (call_webhook.py) when Home Assistant
Cloud is available, and from the coordinator's poll either way. This entity
turns each new call into exactly one event, whichever way saw it first.
"""

from __future__ import annotations

import re

from butterflymx import Call
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import SIGNAL_WEBHOOK_CALL
from .coordinator import ButterflyMXConfigEntry
from .entity import ButterflyMXEntity

EVENT_RING = "ring"
# After a long outage, announce at most this many of the newest calls
MAX_EVENTS_PER_UPDATE = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ButterflyMXConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up an intercom call event entity for each tenant."""
    coordinator = entry.runtime_data
    async_add_entities(IntercomCallEvent(coordinator, tenant) for tenant in coordinator.tenants)


class IntercomCallEvent(ButterflyMXEntity, EventEntity):
    """Fires `ring` when a call appears that was not in the previous poll."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = [EVENT_RING]
    _attr_translation_key = "intercom_call"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, coordinator, tenant) -> None:
        super().__init__(coordinator, tenant)
        self._attr_name = "Intercom Call"
        self._attr_unique_id = f"butterflymx_intercom_call_{tenant.id}"
        # Calls that already existed at startup are history, not new rings
        self._seen: set[str] = {call_key(c.id) for c in self.overview.calls}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        signal = SIGNAL_WEBHOOK_CALL.format(entry_id=self.coordinator.config_entry.entry_id, tenant_id=self._tenant.id)
        self.async_on_remove(async_dispatcher_connect(self.hass, signal, self._handle_webhook_call))

    @callback
    def _handle_webhook_call(self, call: dict[str, str | None]) -> None:
        """A delivery from the ButterflyMX webhook. The poll won't fire it again."""
        if not call.get("id"):
            return  # can't match it to the poll; the refresh it triggered will fire it
        call_id = graphql_call_id(call["id"], self._tenant.id)
        key = call_key(call_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self._trigger_event(EVENT_RING, {**{k: call.get(k) for k in _ATTRS}, "call_id": call_id})
        self.async_write_ha_state()

    @callback
    def _handle_coordinator_update(self) -> None:
        calls = self.overview.calls  # newest first
        new = [c for c in calls if call_key(c.id) not in self._seen]
        self._seen.update(call_key(c.id) for c in calls)
        for call in reversed(new[:MAX_EVENTS_PER_UPDATE]):
            # Write each event, or two calls in one poll would show as one
            self._trigger_event(EVENT_RING, _attributes(call))
            self.async_write_ha_state()
        if not new:
            super()._handle_coordinator_update()


_ATTRS = ("call_id", "status", "type", "device", "logged_at", "image_url")


def call_key(call_id: str) -> str:
    """Same key for the webhook's '277231292' and GraphQL's 'prod-call-277231292'."""
    match = re.fullmatch(r"(?:[a-z]+-call-)?(\d+)", str(call_id))
    return match.group(1) if match else str(call_id)


def graphql_call_id(call_id: str, tenant_id: str) -> str:
    """'277231292' -> 'prod-call-277231292', matching the poll's ids (environment from the tenant id)."""
    if not str(call_id).isdigit():
        return str(call_id)
    match = re.match(r"([a-z]+)-tenant-", str(tenant_id))
    return f"{match.group(1) if match else 'prod'}-call-{call_id}"


def _attributes(call: Call) -> dict[str, str | None]:
    return {
        "call_id": call.id,
        "status": call.status,
        "type": call.type,
        "device": call.device,
        "logged_at": call.logged_at,
        "image_url": call.image_url,
    }
