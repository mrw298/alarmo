"""Test hold-up (panic) alarm confirmation."""

from typing import Any

import pytest

from tests.helpers import advance_time, cleanup_timers, assert_alarm_state
from tests.test_confirmation import ALARM_ENTITY, Setup

HOLD_UP_WINDOW = 600


def _hold_up(setup: Setup, name: str, **kwargs: Any) -> str:
    return setup.sensor(name, sensor_type="hold_up", always_on=True, **kwargs)


async def _start(setup: Setup, **kwargs: Any) -> None:
    """Start with hold-up confirmation enabled, while disarmed."""
    await setup.start(
        expect="disarmed",
        confirmation_enabled=False,
        hold_up_confirmation_enabled=True,
        hold_up_confirmation_time=HOLD_UP_WINDOW,
        **kwargs,
    )


async def _disarm(hass: Any) -> None:
    await hass.services.async_call(
        "alarmo", "disarm", {"entity_id": ALARM_ENTITY, "code": "1234"}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_activation_while_disarmed(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A hold-up activation while disarmed fires the hold-up event."""
    setup = Setup(hass)
    button = _hold_up(setup, "panic_button", room="Hall")
    await _start(setup)

    await setup.trip(button)

    # existing always-on behaviour: the alarm is triggered
    assert_alarm_state(hass, ALARM_ENTITY, "triggered")
    assert setup.event_types() == ["alarmo_hold_up"]
    assert setup.events[0].data["sensors"][0]["entity_id"] == button
    assert setup.attributes["hold_up_status"] == "unconfirmed"
    assert setup.attributes["hold_up_expires"]
    assert "confirmation_status" not in setup.attributes
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_second_device_confirms(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A second hold-up device confirms; repeats are ignored."""
    setup = Setup(hass)
    first = _hold_up(setup, "panic_hall", device="keypad")
    second = _hold_up(setup, "panic_bedroom", device="pendant")
    await _start(setup)

    await setup.trip(first)
    await setup.trip(first)
    assert setup.attributes["hold_up_status"] == "unconfirmed"
    await setup.trip(second)

    assert setup.attributes["hold_up_status"] == "confirmed"
    assert setup.attributes["hold_up_confirmed_by"] == [first, second]
    assert setup.event_types() == ["alarmo_hold_up", "alarmo_hold_up_confirmed"]

    # at the end of the window, the status returns to idle without an event
    await advance_time(hass, HOLD_UP_WINDOW + 1)
    assert setup.attributes["hold_up_status"] == "idle"
    assert setup.event_types() == ["alarmo_hold_up", "alarmo_hold_up_confirmed"]
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_same_device_does_not_confirm(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Two buttons of the same HA device do not confirm each other."""
    setup = Setup(hass)
    first = _hold_up(setup, "button_1", device="keyfob")
    second = _hold_up(setup, "button_2", device="keyfob")
    await _start(setup)

    await setup.trip(first)
    await setup.trip(second)

    assert setup.attributes["hold_up_status"] == "unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_expiry_and_trip_outside_window(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """The window expires once; a later activation starts a new window."""
    setup = Setup(hass)
    first = _hold_up(setup, "panic_hall")
    second = _hold_up(setup, "panic_bedroom")
    await _start(setup)

    await setup.trip(first)
    await advance_time(hass, HOLD_UP_WINDOW + 1)
    assert setup.attributes["hold_up_status"] == "idle"
    await setup.trip(second)

    assert setup.attributes["hold_up_status"] == "unconfirmed"
    assert setup.event_types() == [
        "alarmo_hold_up",
        "alarmo_hold_up_expired",
        "alarmo_hold_up",
    ]
    await cleanup_timers(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper_confirms", [True, False])
async def test_tamper_confirms_only_when_enabled(
    hass: Any, enable_custom_integrations: Any, tamper_confirms: bool
) -> None:
    """A tamper signal confirms a hold-up only when configured."""
    setup = Setup(hass)
    button = _hold_up(setup, "panic_button")
    tamper = setup.sensor("pir_tamper", sensor_type="tamper", always_on=True)
    await _start(setup, hold_up_tamper_confirms=tamper_confirms)

    await setup.trip(button)
    await setup.trip(tamper)

    if tamper_confirms:
        assert setup.attributes["hold_up_status"] == "confirmed"
        assert setup.attributes["hold_up_confirmed_by"] == [button, tamper]
    else:
        assert setup.attributes["hold_up_status"] == "unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_disarm_does_not_reset_hold_up(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Disarming does not reset hold-up tracking."""
    setup = Setup(hass)
    first = _hold_up(setup, "panic_hall")
    second = _hold_up(setup, "panic_bedroom")
    await _start(setup)

    await setup.trip(first)
    await _disarm(hass)
    assert_alarm_state(hass, ALARM_ENTITY, "disarmed")
    assert setup.attributes["hold_up_status"] == "unconfirmed"
    await setup.trip(second)

    assert setup.attributes["hold_up_status"] == "confirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_hold_up_and_intruder_do_not_interfere(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Hold-up devices do not confirm intruders, and the reverse."""
    setup = Setup(hass)
    button = _hold_up(setup, "panic_button")
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    await setup.start(
        hold_up_confirmation_enabled=True, hold_up_confirmation_time=HOLD_UP_WINDOW
    )

    await setup.trip(pir)
    await setup.trip(button)
    assert setup.attributes["confirmation_status"] == "unconfirmed"
    assert setup.attributes["hold_up_status"] == "unconfirmed"

    await setup.trip(door)
    assert setup.attributes["confirmation_status"] == "confirmed"
    assert setup.attributes["confirmed_by"] == [pir, door]
    assert setup.attributes["hold_up_status"] == "unconfirmed"

    await _disarm(hass)
    assert setup.attributes["confirmation_status"] == "idle"
    assert setup.attributes["hold_up_status"] == "unconfirmed"
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_confirmed_hold_up_requires_reset(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """With require reset, a confirmed hold-up blocks arming until reset."""
    setup = Setup(hass)
    first = _hold_up(setup, "panic_hall")
    second = _hold_up(setup, "panic_bedroom")
    await _start(setup, confirmation_require_reset=True)

    await setup.trip(first)
    await setup.trip(second)
    await _disarm(hass)
    await advance_time(hass, HOLD_UP_WINDOW + 1)
    assert setup.attributes["hold_up_status"] == "confirmed"
    assert setup.attributes["reset_required"] is True

    for service in ("arm", "reset_confirmation", "arm"):
        await hass.services.async_call(
            "alarmo",
            service,
            {"entity_id": ALARM_ENTITY, "code": "1234"},
            blocking=True,
        )
        await hass.async_block_till_done()
        if service == "arm" and setup.attributes["reset_required"]:
            assert_alarm_state(hass, ALARM_ENTITY, "disarmed")

    assert setup.attributes["hold_up_status"] == "idle"
    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")
    await cleanup_timers(hass)
