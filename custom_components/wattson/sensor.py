"""The traffic light as a real entity: wattson.py hands its state over through W.PUBLISH (from its thread)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo

from . import DOMAIN, SEMAFORO
from . import wattson as W


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([Semaforo(entry)])


class Semaforo(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "semaforo"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["go", "wait", "stop"]
    _attr_should_poll = False
    _attr_icon = "mdi:traffic-light"
    _unrecorded_attributes = frozenset({MATCH_ALL})    # tens of KB of lists for the dashboard: not for history

    def __init__(self, entry):
        self.entity_id = SEMAFORO      # same id as before: dashboards and automations keep working
        self._attr_unique_id = entry.entry_id + "_semaforo"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name="Wattson",
                                            entry_type=DeviceEntryType.SERVICE, manufacturer="Wattson")

    async def async_added_to_hass(self):
        W.PUBLISH = lambda state, attrs: self.hass.loop.call_soon_threadsafe(self._set, state, attrs)

    async def async_will_remove_from_hass(self):
        W.PUBLISH = None

    @callback
    def _set(self, state, attrs):
        a = dict(attrs)
        a.pop("friendly_name", None)
        self._attr_icon = a.pop("icon", self._attr_icon)
        self._attr_native_value = state
        self._attr_extra_state_attributes = a
        self.async_write_ha_state()
