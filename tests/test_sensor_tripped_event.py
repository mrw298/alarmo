"""Test the alarmo_sensor_tripped event."""

from typing import Any

import pytest
from homeassistant.core import Event

from tests.helpers import (
    cleanup_timers,
    assert_alarm_state,
    setup_alarmo_entry,
    patch_alarmo_integration_dependencies,
)
from tests.factories import AreaFactory, SensorFactory

ALARM_ENTITY = "alarm_control_panel.test_area_1"
DOOR = "binary_sensor.door"
WINDOW = "binary_sensor.window"
NIGHT_ONLY = "binary_sensor.night_only"


async def _setup(hass: Any, events: list[Event], **door_kwargs: Any) -> None:
    """Set up one area with a door, a window and a night-only sensor."""
    area = AreaFactory.create_area(
        modes=["armed_away"], armed_away_exit_time=0, armed_away_entry_time=0
    )
    sensors = [
        SensorFactory.create_door_sensor(
            entity_id=DOOR, use_entry_delay=False, **door_kwargs
        ),
        SensorFactory.create_window_sensor(entity_id=WINDOW, use_entry_delay=False),
        SensorFactory.create_window_sensor(entity_id=NIGHT_ONLY, modes=["armed_night"]),
    ]
    storage, entry = setup_alarmo_entry(
        hass, areas=[area], sensors=sensors, entry_id="test_sensor_tripped"
    )
    with patch_alarmo_integration_dependencies(storage):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    for sensor in (DOOR, WINDOW, NIGHT_ONLY):
        hass.states.async_set(sensor, "off")
    await hass.async_block_till_done()
    hass.bus.async_listen("alarmo_sensor_tripped", events.append)


async def _arm(hass: Any) -> None:
    await hass.services.async_call(
        "alarmo", "arm", {"entity_id": ALARM_ENTITY, "code": "1234"}, blocking=True
    )
    await hass.async_block_till_done()
    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")


async def _set(hass: Any, entity_id: str, state: str) -> None:
    hass.states.async_set(entity_id, state)
    await hass.async_block_till_done()
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_trip_while_armed_fires_event(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A trip while armed fires one event with the sensor details."""
    events: list[Event] = []
    await _setup(hass, events)
    await _arm(hass)

    await _set(hass, DOOR, "on")

    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    assert len(events) == 1
    assert events[0].data == {
        "area_id": "area_1",
        "entity_id": ALARM_ENTITY,
        "sensor": DOOR,
        "sensor_type": "door",
        "state": "open",
        "alarm_state": "armed_away",
    }
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_trip_while_triggered_fires_event(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A second sensor tripping while triggered is still reported."""
    events: list[Event] = []
    await _setup(hass, events)
    await _arm(hass)

    await _set(hass, DOOR, "on")
    await _set(hass, WINDOW, "on")

    assert [e.data["sensor"] for e in events] == [DOOR, WINDOW]
    assert events[1].data["alarm_state"] == "triggered"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_no_event_for_inactive_or_closing_sensors(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Sensors that would not trigger the alarm are not reported."""
    events: list[Event] = []
    await _setup(hass, events)

    # disarmed: a normal sensor does not trip
    await _set(hass, DOOR, "on")
    await _set(hass, DOOR, "off")
    assert events == []

    await _arm(hass)
    # sensor not active in armed_away
    await _set(hass, NIGHT_ONLY, "on")
    assert events == []

    await _set(hass, DOOR, "on")
    # closing a sensor is not a trip
    await _set(hass, DOOR, "off")
    # inactive sensor while triggered is not a trip either
    await _set(hass, NIGHT_ONLY, "off")
    await _set(hass, NIGHT_ONLY, "on")
    assert [e.data["sensor"] for e in events] == [DOOR]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_unavailable_trip_reports_unavailable_state(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A trip caused by a sensor going unavailable is marked as such."""
    events: list[Event] = []
    await _setup(hass, events, trigger_unavailable=True)
    await _arm(hass)

    await _set(hass, DOOR, "unavailable")

    assert len(events) == 1
    assert events[0].data["state"] == "unavailable"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_always_on_sensor_trip_while_disarmed(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """An always-on sensor trip while disarmed is reported."""
    events: list[Event] = []
    await _setup(hass, events, always_on=True)

    await _set(hass, DOOR, "on")

    assert len(events) == 1
    assert events[0].data["alarm_state"] == "disarmed"
    await cleanup_timers(hass)
