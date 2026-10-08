"""Sensor platform for MTTL-W01 WiFi.

Three of the readings here are deliberately NOT what a naive integration would
create, and each omission is a decision rather than an oversight:

**No current sensor.** The strip exposes no current reading at all. This was
measured by sweeping all five firmware channels: every one returns the same mains
voltage and nothing else. A claim of "under 50000 means milliamps" exists in an
upstream project and is false on real hardware. Since there is no current, watts
can never be converted to amps, and a sensor stuck at "unavailable" forever is
noise pretending to be a feature.

**No `device_class: power` on the watt sensor.** That class is what puts a value
into Home Assistant's energy dashboard and long-term statistics as though it were
a calibrated measurement. It is not: the vendor divides by 1000, and a measured
60 W load read back as 16.75 W. This sensor therefore has a unit and a state
class but deliberately no device class, and its name says "unverified" out loud.
The raw integer ships beside it, because the raw integer is the only part of this
that is not a guess.

**No overload or overheat sensor.** Status fields 3 and 4 of the strip's
12-field block read `on` on every channel, including a completely empty socket.
An earlier version of this project's code interpreted them as safety flags and
produced `overload: true` on an empty outlet, which is how the mistake was
caught. They are carried as unlabelled diagnostics instead.

Volts and RSSI come from a different route entirely - an active query rather than
the status block - and both are real measurements, so they do get their device
classes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, SOCKET_COUNT
from .coordinator import MttlW01ProbeCoordinator, MttlW01StateCoordinator


@dataclass(frozen=True, kw_only=True)
class MttlW01SensorDescription(SensorEntityDescription):
    """A sensor plus where its value comes from."""

    value: Callable[[dict[str, Any]], Any]
    socket: bool = False


# -- per outlet ----------------------------------------------------------------

SOCKET_SENSORS: tuple[MttlW01SensorDescription, ...] = (
    MttlW01SensorDescription(
        key="power_w",
        translation_key="socket_power_unverified",
        device_class=None,  # deliberately absent - see module docstring
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda s: s.get("power_w"),
        socket=True,
    ),
    MttlW01SensorDescription(
        key="power_raw",
        translation_key="socket_power_raw",
        device_class=None,
        native_unit_of_measurement=None,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.get("power_raw"),
        socket=True,
    ),
    MttlW01SensorDescription(
        key="energy_kwh",
        translation_key="socket_energy_unverified",
        device_class=None,  # deliberately absent - see module docstring
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=3,
        value=lambda s: s.get("energy_kwh"),
        socket=True,
    ),
    MttlW01SensorDescription(
        key="temp_c",
        translation_key="socket_temperature",
        # Temperature really is what field 11 holds, so this one is honest.
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda s: s.get("temp_c"),
        socket=True,
    ),
    MttlW01SensorDescription(
        key="state_code",
        translation_key="socket_state_code",
        device_class=None,
        native_unit_of_measurement=None,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda s: s.get("state_code"),
        socket=True,
    ),
)

# -- per strip -----------------------------------------------------------------

PROBE_SENSORS: tuple[MttlW01SensorDescription, ...] = (
    MttlW01SensorDescription(
        key="voltage",
        translation_key="mains_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda p: p.get("voltage_v"),
    ),
    MttlW01SensorDescription(
        key="rssi",
        translation_key="signal_strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda p: p.get("rssi_dbm"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensors for every known strip."""
    runtime = entry.runtime_data
    state: MttlW01StateCoordinator = runtime.state
    probe: MttlW01ProbeCoordinator = runtime.probe
    created: set[str] = set()

    async def _add(devids: list[str]) -> None:
        fresh = [d for d in devids if d not in created]
        if not fresh:
            return
        created.update(fresh)
        entities: list[MttlW01Sensor] = []
        for devid in fresh:
            for description in PROBE_SENSORS:
                entities.append(MttlW01Sensor(state, probe, devid, description))
            for number in range(1, SOCKET_COUNT + 1):
                for description in SOCKET_SENSORS:
                    entities.append(
                        MttlW01Sensor(state, probe, devid, description, number)
                    )
        async_add_entities(entities)

    entry.async_on_unload(state.async_register_platform(_add))
    await _add(state.known_strips)


class MttlW01Sensor(CoordinatorEntity[MttlW01StateCoordinator], SensorEntity):
    """One reading, from either the status block or the measurement query."""

    _attr_has_entity_name = True

    def __init__(
        self,
        state: MttlW01StateCoordinator,
        probe: MttlW01ProbeCoordinator,
        devid: str,
        description: MttlW01SensorDescription,
        number: int | None = None,
    ) -> None:
        super().__init__(state)
        self.entity_description = description
        self._devid = devid
        self._number = number
        self._probe = probe

        doc = state.device_doc(devid) or {}
        simulated = bool(doc.get("simulated")) or devid == "SIM"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, devid)},
            "manufacturer": "TONLY / LG-U+",
            "model": str(doc.get("model") or "MTTL-W01"),
            "name": ("MTTL-W01 (simulator)" if simulated
                     else f"MTTL-W01 {devid}"),
            "sw_version": str(doc.get("firmware") or ""),
        }

        if number is None:
            self._attr_unique_id = f"{devid}_{description.key}"
        else:
            self._attr_unique_id = f"{devid}_socket_{number}_{description.key}"
            self._attr_translation_placeholders = {"socket_number": str(number)}

    @property
    def _is_real(self) -> bool:
        return self.coordinator.is_real_strip(self._devid)

    @property
    def available(self) -> bool:
        if not self.coordinator.last_update_success:
            return False
        if not self._is_real:
            return False

        if self._number is None:
            # Strip-level measurement. A strip that is simply not answering has
            # no voltage right now; that is not an error, it is an absence.
            return self._probe.last_update_success and self._probe.measurement(self._devid) is not None

        if not self.coordinator.is_settled(self._devid):
            return False
        return self.coordinator.socket(self._devid, self._number) is not None

    @property
    def native_value(self) -> Any:
        source: dict[str, Any] | None
        if self._number is None:
            source = self._probe.measurement(self._devid)
        else:
            source = self.coordinator.socket(self._devid, self._number)

        if source is None:
            return None

        value = self.entity_description.value(source)
        # Under the simulator these readings are structurally absent, and a
        # fabricated 0 would look like a measurement of zero.
        if source.get("simulated") and self.entity_description.key in (
            "power_w",
            "power_raw",
            "energy_kwh",
            "temp_c",
        ):
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._number is not None:
            return {"socket": self._number}

        probe = self._probe.measurement(self._devid)
        if not probe:
            return {}
        attrs: dict[str, Any] = {}
        # The spread between the four channels is published rather than implied,
        # so agreement is visible. A single number hides a disagreement.
        if probe.get("voltage_spread_v") is not None:
            attrs["voltage_spread_v"] = probe.get("voltage_spread_v")
        if probe.get("voltage_channels") is not None:
            attrs["voltage_channels"] = probe.get("voltage_channels")
        # Always false on this hardware. Reported so the absence is a stated
        # fact rather than a sensor that mysteriously does not exist.
        attrs["current_available"] = bool(probe.get("current_available"))
        if probe.get("note"):
            attrs["note"] = probe.get("note")
        return attrs


__all__ = ["MttlW01Sensor", "async_setup_entry"]