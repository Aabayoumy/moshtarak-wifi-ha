"""Binary sensors for Moshtarak WiFi.

The important one is `Real strip connected`. It exists because of a specific,
measured trap: with no hardware attached, the controller answers `/api/state`
with four healthy sockets, `reachable: true` and `settled: true`, because its
built-in simulator reports as device `SIM`. Neither of those flags means a
person's power strip is answering.

This sensor is the honest answer to "is this actually my hardware?", and it is
also what keeps the switches and other sensors unavailable while nothing real is
connected. An integration that showed four working-looking switches in that state
would be showing a facade - the one kind of defect this project treats as serious.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOCKET_COUNT
from .coordinator import MoshtarakStateCoordinator


@dataclass(frozen=True, kw_only=True)
class MoshtarakBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor plus where its value comes from."""

    value: Callable[[Any], bool]
    socket: bool = False


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors for every known strip."""
    runtime = entry.runtime_data
    coordinator: MoshtarakStateCoordinator = runtime.state
    created: set[str] = set()

    async def _add(devids: list[str]) -> None:
        fresh = [d for d in devids if d not in created]
        if not fresh:
            return
        created.update(fresh)
        entities: list[MoshtarakBinarySensor] = []
        for devid in fresh:
            entities.append(
                MoshtarakBinarySensor(coordinator, devid, REAL_STRIP)
            )
            for number in range(1, SOCKET_COUNT + 1):
                entities.append(
                    MoshtarakBinarySensor(coordinator, devid, DRAWS_CURRENT, number)
                )
                entities.append(
                    MoshtarakBinarySensor(coordinator, devid, PROTECTED_OUTLET, number)
                )
        async_add_entities(entities)

    entry.async_on_unload(coordinator.async_register_platform(_add))
    await _add(coordinator.known_strips)


class MoshtarakBinarySensor(
    CoordinatorEntity[MoshtarakStateCoordinator], BinarySensorEntity
):
    """One binary reading about a strip or one of its outlets."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: MoshtarakStateCoordinator,
        devid: str,
        description: MoshtarakBinarySensorDescription,
        number: int | None = None,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._devid = devid
        self._number = number

        doc = coordinator.device_doc(devid) or {}
        simulated = bool(doc.get("simulated")) or devid == "SIM"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, devid)},
            "manufacturer": "TONLY / LG-U+",
            "model": str(doc.get("model") or "MTTL-W01"),
            "name": ("MTTL-W01 (simulator)" if simulated
                     else f"MTTL-W01 {devid}"),
        }

        if number is None:
            self._attr_unique_id = f"{devid}_{description.key}"
        else:
            self._attr_unique_id = f"{devid}_socket_{number}_{description.key}"
            self._attr_translation_placeholders = {"socket_number": str(number)}

    @property
    def _entry(self) -> dict[str, Any] | None:
        if self._number is None:
            return None
        return self.coordinator.socket(self._devid, self._number)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        # "Real strip connected" must stay available when nothing real is
        # connected - reporting that fact is its entire job.
        if self.entity_description.key == "real_strip":
            return True
        # Protection comes from saved configuration, not from a live reading, so
        # it stays true while the strip is asleep. A lock indicator that blinks
        # out exactly when someone might need to trust it would be worse than
        # useless.
        if self.entity_description.key == "protected":
            return True
        if not self.coordinator.is_real_strip(self._devid):
            return False
        if self._number is not None and self._entry is None:
            return False
        return True

    @property
    def is_on(self) -> bool:
        if self.entity_description.key == "real_strip":
            return self.coordinator.is_real_strip(self._devid)
        if self.entity_description.key == "protected":
            entry = self._entry
            if entry is not None:
                return bool(entry.get("protected"))
            return self.coordinator.is_protected(self._devid, int(self._number))
        return bool(self.entity_description.value(self._entry or {}))


#: Is a real (non-simulated) strip connected at all?
REAL_STRIP = MoshtarakBinarySensorDescription(
    key="real_strip",
    translation_key="real_strip_connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    value=lambda c: c.has_real_strip(),
)

#: Something is drawing power through this outlet.
DRAWS_CURRENT = MoshtarakBinarySensorDescription(
    key="draws_current",
    translation_key="socket_draws_current",
    device_class=BinarySensorDeviceClass.POWER,
    socket=True,
    value=lambda s: bool(s.get("draws_current")),
)

#: This outlet refuses to be switched off.
PROTECTED_OUTLET = MoshtarakBinarySensorDescription(
    key="protected",
    translation_key="socket_protected",
    device_class=None,
    entity_category=EntityCategory.DIAGNOSTIC,
    socket=True,
    value=lambda s: bool(s.get("protected")),
)


__all__ = ["MoshtarakBinarySensor", "async_setup_entry"]