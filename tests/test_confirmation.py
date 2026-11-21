"""Test intruder alarm confirmation (PD 6662 / BS 8243 style)."""

from typing import Any

import pytest
from homeassistant.core import Event
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.helpers import (
    advance_time,
    cleanup_timers,
    assert_alarm_state,
    setup_alarmo_entry,
    patch_alarmo_integration_dependencies,
)
from tests.factories import AreaFactory, SensorFactory
from custom_components.alarmo.store import AlarmoStorage

ALARM_ENTITY = "alarm_control_panel.test_area_1"
WINDOW = 300
TRIGGER_TIME = 120
ENTRY_TIME = 30
EVENTS = [
    "alarmo_unconfirmed",
    "alarmo_confirmed",
    "alarmo_confirmation_expired",
]


class Setup:
    """Set up an area, its sensors and their HA areas and devices."""

    def __init__(self, hass: Any) -> None:
        """Initialize."""
        self.hass = hass
        self.sensors: list[dict[str, Any]] = []
        self.events: list[Event] = []
        self._source = MockConfigEntry(domain="test")
        self._source.add_to_hass(hass)

    def sensor(
        self,
        name: str,
        room: str | None = None,
        device: str | None = None,
        room_on_device: bool = False,
        sensor_type: str = "motion",
        device_class: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Register a sensor in HA and in Alarmo, return its entity_id."""
        area_id = ar.async_get(self.hass).async_get_or_create(room).id if room else None
        device_id = None
        if device:
            device_entry = dr.async_get(self.hass).async_get_or_create(
                config_entry_id=self._source.entry_id, identifiers={("test", device)}
            )
            if room_on_device:
                dr.async_get(self.hass).async_update_device(
                    device_entry.id, area_id=area_id
                )
            device_id = device_entry.id
        entity = er.async_get(self.hass).async_get_or_create(
            "binary_sensor",
            "test",
            name,
            suggested_object_id=name,
            device_id=device_id,
        )
        if room and not room_on_device:
            er.async_get(self.hass).async_update_entity(
                entity.entity_id, area_id=area_id
            )
        config = SensorFactory.create_sensor(
            entity_id=entity.entity_id,
            name=name,
            modes=["armed_away"],
            use_entry_delay=False,
        )
        config.update({"type": sensor_type, **kwargs})
        self.sensors.append(config)
        attributes = {"friendly_name": name.replace("_", " ").title()}
        if device_class:
            attributes["device_class"] = device_class
        self.hass.states.async_set(entity.entity_id, "off", attributes)
        return entity.entity_id

    async def start(
        self,
        master: bool = False,
        users: dict[str, Any] | None = None,
        expect: str = "armed_away",
        **area_kwargs: Any,
    ) -> None:
        """Start Alarmo with confirmation enabled and arm the area."""
        area = AreaFactory.create_area(
            modes=["armed_away"],
            armed_away_exit_time=0,
            armed_away_entry_time=ENTRY_TIME,
            armed_away_trigger_time=TRIGGER_TIME,
        )
        area.update(
            {
                "confirmation_enabled": True,
                "confirmation_time": WINDOW,
                "confirmation_rule": "different_sensor",
                **area_kwargs,
            }
        )
        # the mock storage shares one config dict between areas, so keep the
        # second area identical apart from its id
        areas = [area, {**area, "area_id": "area_2", "name": "Test Area 2"}]
        areas = areas if master else [area]
        storage, entry = setup_alarmo_entry(
            self.hass,
            areas=areas,
            sensors=self.sensors,
            entry_id="test_confirm",
            master_enabled=master,
            users=users,
        )
        with patch_alarmo_integration_dependencies(storage):
            await self.hass.config_entries.async_setup(entry.entry_id)
            await self.hass.async_block_till_done()
        for event in EVENTS:
            self.hass.bus.async_listen(event, self.events.append)
        await self.hass.services.async_call(
            "alarmo",
            "arm",
            {"entity_id": ALARM_ENTITY, "code": "1234"},
            blocking=True,
        )
        await self.hass.async_block_till_done()
        assert_alarm_state(self.hass, ALARM_ENTITY, expect)

    async def trip(self, entity_id: str, state: str = "on") -> None:
        """Trip a sensor, then restore it."""
        attributes = self.hass.states.get(entity_id).attributes
        self.hass.states.async_set(entity_id, state, attributes)
        await self.hass.async_block_till_done()
        await self.hass.async_block_till_done()
        self.hass.states.async_set(entity_id, "off", attributes)
        await self.hass.async_block_till_done()

    @property
    def attributes(self) -> dict[str, Any]:
        """Return the alarm entity attributes."""
        return self.hass.states.get(ALARM_ENTITY).attributes

    @property
    def status(self) -> str:
        """Return the confirmation status."""
        return self.attributes["confirmation_status"]

    def event_types(self) -> list[str]:
        """Return the types of the captured events."""
        return [e.event_type for e in self.events]


@pytest.mark.asyncio
async def test_single_trip_triggers_without_confirmation_enabled(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """With confirmation disabled, nothing changes."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    await setup.start(confirmation_enabled=False)

    await setup.trip(pir)

    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    assert "confirmation_status" not in setup.attributes
    assert setup.events == []
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_single_trip_triggers_and_is_unconfirmed(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """With confirmation enabled, a single trip triggers as before."""
    setup = Setup(hass)
    pir = setup.sensor("pir", room="Lounge")
    await setup.start()
    assert setup.status == "idle"

    await setup.trip(pir)

    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    assert setup.status == "unconfirmed"
    assert setup.attributes["confirmation_expires"]
    assert setup.event_types() == ["alarmo_unconfirmed"]
    data = setup.events[0].data
    assert data["area_id"] == "area_1"
    assert data["entity_id"] == ALARM_ENTITY
    assert data["rule"] == "different_sensor"
    assert data["sensors"] == [
        {
            "entity_id": pir,
            "ha_area_id": ar.async_get(hass).async_get_area_by_name("Lounge").id,
            "device_id": None,
        }
    ]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_different_sensor_confirms_once(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A second independent sensor confirms; events fire exactly once."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    window = setup.sensor("window", sensor_type="window")
    await setup.start()

    await setup.trip(pir)
    await setup.trip(pir)
    assert setup.status == "unconfirmed"
    await setup.trip(door)
    assert setup.status == "confirmed"
    assert setup.attributes["confirmed_by"] == [pir, door]
    assert setup.attributes["confirmation_expires"] is None
    await setup.trip(window)

    assert setup.event_types() == ["alarmo_unconfirmed", "alarmo_confirmed"]
    assert [el["entity_id"] for el in setup.events[1].data["sensors"]] == [pir, door]
    await advance_time(hass, WINDOW + 1)
    assert setup.event_types() == ["alarmo_unconfirmed", "alarmo_confirmed"]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_window_expiry(hass: Any, enable_custom_integrations: Any) -> None:
    """Without a second sensor the window expires once, a later trip starts over."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    await setup.start()

    await setup.trip(pir)
    await advance_time(hass, WINDOW + 1)

    assert setup.status == "idle"
    assert setup.event_types() == ["alarmo_unconfirmed", "alarmo_confirmation_expired"]
    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")

    await setup.trip(door)
    assert setup.status == "unconfirmed"
    assert setup.event_types()[-1] == "alarmo_unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rule", "rooms", "confirmed"),
    [
        ("different_area", ("Lounge", "Lounge"), False),
        ("different_area", ("Lounge", "Hall"), True),
        ("different_area", ("Lounge", None), True),
        ("same_area", ("Lounge", "Lounge"), True),
        ("same_area", ("Lounge", "Hall"), False),
        ("same_area", (None, None), False),
        ("different_sensor", ("Lounge", "Lounge"), True),
    ],
)
async def test_area_rules(
    hass: Any,
    enable_custom_integrations: Any,
    rule: str,
    rooms: tuple[str | None, str | None],
    confirmed: bool,
) -> None:
    """The configured rule decides whether two rooms confirm."""
    setup = Setup(hass)
    first = setup.sensor("first", room=rooms[0])
    second = setup.sensor("second", room=rooms[1])
    await setup.start(confirmation_rule=rule)

    await setup.trip(first)
    await setup.trip(second)

    assert setup.status == ("confirmed" if confirmed else "unconfirmed")
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_same_area_confirms_on_any_pair(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Lounge, then hall, then hall door: confirms on the hall pair."""
    setup = Setup(hass)
    lounge = setup.sensor("lounge_pir", room="Lounge")
    hall = setup.sensor("hall_pir", room="Hall")
    hall_door = setup.sensor("hall_door", room="Hall", sensor_type="door")
    await setup.start(confirmation_rule="same_area")

    await setup.trip(lounge)
    await setup.trip(hall)
    assert setup.status == "unconfirmed"
    await setup.trip(hall_door)

    assert setup.status == "confirmed"
    assert setup.attributes["confirmed_by"] == [hall, hall_door]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_area_is_resolved_from_device(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """An entity without an area uses the area of its device."""
    setup = Setup(hass)
    first = setup.sensor("first", room="Lounge", device="dev1", room_on_device=True)
    second = setup.sensor("second", room="Lounge")
    await setup.start(confirmation_rule="same_area")

    await setup.trip(first)
    await setup.trip(second)

    assert setup.status == "confirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["different_sensor", "different_area", "same_area"])
async def test_same_device_never_confirms(
    hass: Any, enable_custom_integrations: Any, rule: str
) -> None:
    """Two entities of one physical device do not confirm each other."""
    setup = Setup(hass)
    pir = setup.sensor("dual_pir", room="Lounge", device="dual")
    microwave = setup.sensor("dual_mw", room="Hall", device="dual")
    await setup.start(confirmation_rule=rule)

    await setup.trip(pir)
    await setup.trip(microwave)

    assert setup.status == "unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_kwargs", "state"),
    [
        ({"trigger_unavailable": True}, "unavailable"),
        ({"sensor_type": "tamper"}, "on"),
        ({"device_class": "tamper"}, "on"),
        ({"sensor_type": "environmental"}, "on"),
    ],
)
async def test_non_qualifying_trips_do_not_confirm(
    hass: Any,
    enable_custom_integrations: Any,
    second_kwargs: dict[str, Any],
    state: str,
) -> None:
    """Unavailable, tamper and environmental trips never count."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    other = setup.sensor("other", **second_kwargs)
    await setup.start()

    await setup.trip(pir)
    await setup.trip(other, state)

    assert setup.status == "unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_entry_route(hass: Any, enable_custom_integrations: Any) -> None:
    """After entry starts, entry route sensors do not confirm, others do."""
    setup = Setup(hass)
    door = setup.sensor(
        "front_door", sensor_type="door", use_entry_delay=True, entry_route=True
    )
    hall = setup.sensor("hall_pir", use_entry_delay=True, entry_route=True)
    lounge = setup.sensor("lounge_pir")
    await setup.start()

    await setup.trip(door)
    assert_alarm_state(hass, ALARM_ENTITY, "pending")
    assert setup.status == "idle"
    await setup.trip(hall)
    await advance_time(hass, ENTRY_TIME + 1)

    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    assert setup.status == "unconfirmed"
    assert [el["entity_id"] for el in setup.events[0].data["sensors"]] == [door]

    await setup.trip(hall)
    assert setup.status == "unconfirmed"
    await setup.trip(lounge)
    assert setup.status == "confirmed"
    assert setup.attributes["confirmed_by"] == [door, lounge]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_disarm_during_entry_does_not_report(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Disarming within the entry time leaves no trace."""
    setup = Setup(hass)
    door = setup.sensor("front_door", sensor_type="door", use_entry_delay=True)
    await setup.start()

    await setup.trip(door)
    await hass.services.async_call(
        "alarmo", "disarm", {"entity_id": ALARM_ENTITY, "code": "1234"}, blocking=True
    )
    await hass.async_block_till_done()
    await advance_time(hass, ENTRY_TIME + 1)

    assert setup.status == "idle"
    assert setup.events == []
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_confirmation_after_trigger_time_retriggers(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """When the alarm is armed again, a trip in the window confirms immediately."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door", use_entry_delay=True)
    await setup.start()

    await setup.trip(pir)
    await advance_time(hass, TRIGGER_TIME + 1)
    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")
    assert setup.status == "unconfirmed"

    await setup.trip(door)

    assert setup.status == "confirmed"
    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_disarm_resets(hass: Any, enable_custom_integrations: Any) -> None:
    """Disarming resets the tracking."""
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    await setup.start()

    await setup.trip(pir)
    await hass.services.async_call(
        "alarmo", "disarm", {"entity_id": ALARM_ENTITY, "code": "1234"}, blocking=True
    )
    await hass.async_block_till_done()
    assert setup.status == "idle"
    assert setup.attributes["confirmed_by"] == []

    await setup.trip(door)
    await advance_time(hass, WINDOW + 1)
    assert setup.event_types() == ["alarmo_unconfirmed"]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_confirmed_notification_with_wildcard(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A notification on the confirmed trigger runs with the confirming sensors."""
    calls = []
    hass.services.async_register("notify", "test", calls.append)
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    await setup.start()
    hass.data["alarmo"]["automation_handler"]._config = {
        "1": {
            "automation_id": "1",
            "type": "notification",
            "name": "confirmed",
            "enabled": True,
            "triggers": [{"event": "confirmed", "area": "area_1", "modes": []}],
            "actions": [
                {
                    "service": "notify.test",
                    "entity_id": None,
                    "data": {"message": "Confirmed by {{confirmed_by}}"},
                }
            ],
        }
    }

    await setup.trip(pir)
    await setup.trip(door)
    await hass.async_block_till_done()

    assert len(calls) == 1
    assert calls[0].data["message"] == "Confirmed by Pir, Door"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_master_aggregates_and_runs_master_notifications(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """The master shows the confirmation of its areas and runs its own actions."""
    calls = []
    hass.services.async_register("notify", "test", calls.append)
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    await setup.start(master=True)
    hass.data["alarmo"]["automation_handler"]._config = {
        "1": {
            "automation_id": "1",
            "type": "notification",
            "name": "confirmed",
            "enabled": True,
            "triggers": [{"event": "confirmed", "area": None, "modes": []}],
            "actions": [
                {"service": "notify.test", "entity_id": None, "data": {"message": "x"}}
            ],
        }
    }

    await setup.trip(pir)
    master = hass.states.get("alarm_control_panel.master").attributes
    assert master["confirmation_status"] == "unconfirmed"
    await setup.trip(door)
    await hass.async_block_till_done()

    master = hass.states.get("alarm_control_panel.master").attributes
    assert master["confirmation_status"] == "confirmed"
    assert master["confirmed_by"] == [pir, door]
    assert len(calls) == 1
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_store_loads_old_data_with_confirmation_disabled(hass: Any) -> None:
    """Areas and sensors stored before this feature load with it disabled."""
    stored_data = {
        "config": {
            "code_arm_required": False,
            "code_mode_change_required": False,
            "code_disarm_required": False,
            "code_format": "number",
            "disarm_after_trigger": False,
        },
        "areas": [{"area_id": "area_1", "name": "Test Area", "modes": {}}],
        "sensors": [
            {"entity_id": "binary_sensor.door", "type": "door", "area": "area_1"}
        ],
        "automations": [],
    }
    storage = AlarmoStorage(hass)
    storage._store.async_load = lambda: _async_return(stored_data)

    await storage.async_load()

    area = storage.async_get_area("area_1")
    assert area["confirmation_enabled"] is False
    assert area["confirmation_time"] == 1800
    assert area["confirmation_rule"] == "different_sensor"
    assert storage.async_get_sensor("binary_sensor.door")["entry_route"] is False


async def _async_return(value: Any) -> Any:
    return value
