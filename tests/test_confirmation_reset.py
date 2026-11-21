"""Test the reset required after a confirmed alarm."""

from typing import Any

import pytest
from homeassistant.core import Event, State
from pytest_homeassistant_custom_component.common import mock_restore_cache

from tests.helpers import cleanup_timers, assert_alarm_state
from tests.factories import UserFactory
from tests.test_confirmation import ALARM_ENTITY, Setup

MASTER_ENTITY = "alarm_control_panel.master"


async def _confirmed(hass: Any, **kwargs: Any) -> tuple[Setup, list[Event]]:
    """Confirm an alarm with require reset enabled, then disarm."""
    failures: list[Event] = []
    hass.bus.async_listen("alarmo_failed_to_arm", failures.append)
    hass.bus.async_listen("alarmo_confirmation_reset", failures.append)
    setup = Setup(hass)
    pir = setup.sensor("pir")
    door = setup.sensor("door", sensor_type="door")
    kwargs.setdefault("confirmation_require_reset", True)
    await setup.start(**kwargs)
    await setup.trip(pir)
    await setup.trip(door)
    assert setup.status == "confirmed"
    await _call(hass, "disarm", "1234")
    assert_alarm_state(hass, ALARM_ENTITY, "disarmed")
    return setup, failures


async def _call(
    hass: Any, service: str, code: str, entity_id: str = ALARM_ENTITY
) -> None:
    await hass.services.async_call(
        "alarmo", service, {"entity_id": entity_id, "code": code}, blocking=True
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_arming_blocked_until_reset(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """A confirmed alarm blocks arming until it is reset with a valid code."""
    setup, events = await _confirmed(hass)
    assert setup.attributes["reset_required"] is True
    assert setup.status == "confirmed"

    await _call(hass, "arm", "1234")
    assert_alarm_state(hass, ALARM_ENTITY, "disarmed")
    assert events[-1].event_type == "alarmo_failed_to_arm"
    assert events[-1].data["reason"] == "reset_required"
    assert events[-1].data["sensors"] == []

    await _call(hass, "reset_confirmation", "1234")
    assert setup.attributes["reset_required"] is False
    assert setup.status == "idle"
    assert events[-1].event_type == "alarmo_confirmation_reset"
    assert events[-1].data["changed_by"] == "Test User"

    await _call(hass, "arm", "1234")
    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")
    await cleanup_timers(hass)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user",
    [
        None,
        UserFactory.create_user(code="5555", can_disarm=False),
        UserFactory.create_user(code="5555", area_limit=["area_2"]),
    ],
    ids=["invalid_code", "no_disarm_permission", "other_area"],
)
async def test_reset_rejected(
    hass: Any, enable_custom_integrations: Any, user: dict[str, Any] | None
) -> None:
    """A wrong code or insufficient permission does not reset."""
    users = {"admin": UserFactory.create_user(user_id="admin", code="1234")}
    if user:
        users["user"] = {**user, "user_id": "user", "name": "User"}
    setup, _events = await _confirmed(hass, users=users)

    await _call(hass, "reset_confirmation", "5555")

    assert setup.attributes["reset_required"] is True
    await _call(hass, "arm", "1234")
    assert_alarm_state(hass, ALARM_ENTITY, "disarmed")
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_without_require_reset_arming_is_unaffected(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """Without require reset, a confirmed alarm does not block arming."""
    setup, _events = await _confirmed(hass, confirmation_require_reset=False)
    assert setup.attributes["reset_required"] is False
    assert setup.status == "idle"

    await _call(hass, "arm", "1234")

    assert_alarm_state(hass, ALARM_ENTITY, "armed_away")
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_master_arming_blocked_and_reset(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """The master cannot arm while an area needs a reset, and can reset it."""
    setup, events = await _confirmed(hass, master=True)
    assert hass.states.get(MASTER_ENTITY).attributes["reset_required"] is True

    await _call(hass, "arm", "1234", MASTER_ENTITY)
    assert_alarm_state(hass, ALARM_ENTITY, "disarmed")
    assert events[-1].data["reason"] == "reset_required"

    await _call(hass, "reset_confirmation", "1234", MASTER_ENTITY)
    assert setup.attributes["reset_required"] is False
    assert hass.states.get(MASTER_ENTITY).attributes["reset_required"] is False
    await cleanup_timers(hass)


@pytest.mark.asyncio
async def test_reset_required_survives_restart(
    hass: Any, enable_custom_integrations: Any
) -> None:
    """The reset required state is restored after a restart."""
    mock_restore_cache(
        hass,
        [
            State(
                ALARM_ENTITY,
                "disarmed",
                {"reset_required": True, "confirmed_by": ["binary_sensor.pir"]},
            )
        ],
    )
    events: list[Event] = []
    hass.bus.async_listen("alarmo_failed_to_arm", events.append)
    setup = Setup(hass)
    setup.sensor("pir")
    await setup.start(confirmation_require_reset=True, expect="disarmed")
    await _call(hass, "arm", "1234")

    assert setup.attributes["reset_required"] is True
    assert setup.status == "confirmed"
    assert setup.attributes["confirmed_by"] == ["binary_sensor.pir"]
    assert events[-1].data["reason"] == "reset_required"
    await cleanup_timers(hass)
