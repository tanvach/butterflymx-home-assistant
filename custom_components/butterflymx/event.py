"""Event entity that fires once for each new ButterflyMX intercom call.

ButterflyMX has no feed of new calls for a resident account: its only GraphQL
subscription (callStatusUpdates) needs the ID of a call that is still active,
which the official apps get from a push notification. So new calls are found
by the coordinator's poll, and this entity turns each new call ID into one
event, which is a better automation trigger than a sensor changing.
"""

from __future__ import annotations

from butterflymx import Call
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

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
        self._seen: set[str] = {c.id for c in self.overview.calls}

    @callback
    def _handle_coordinator_update(self) -> None:
        calls = self.overview.calls  # newest first
        new = [c for c in calls if c.id not in self._seen]
        self._seen.update(c.id for c in calls)
        for call in reversed(new[:MAX_EVENTS_PER_UPDATE]):
            # Write each event, or two calls in one poll would show as one
            self._trigger_event(EVENT_RING, _attributes(call))
            self.async_write_ha_state()
        if not new:
            super()._handle_coordinator_update()


def _attributes(call: Call) -> dict[str, str | None]:
    return {
        "call_id": call.id,
        "status": call.status,
        "type": call.type,
        "device": call.device,
        "logged_at": call.logged_at,
        "image_url": call.image_url,
    }
