"""Alarm confirmation (PD 6662 / BS 8243 style) for Alarmo areas."""

import logging
import datetime
import itertools

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant, callback
from homeassistant.const import STATE_OPEN
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.dispatcher import dispatcher_send
from homeassistant.components.alarm_control_panel import AlarmControlPanelState

from . import const
from .sensors import (
    SENSOR_TYPE_TAMPER,
    SENSOR_TYPE_HOLD_UP,
    SENSOR_TYPE_ENVIRONMENTAL,
)

_LOGGER = logging.getLogger(__name__)

STATUS_IDLE = "idle"
STATUS_UNCONFIRMED = "unconfirmed"
STATUS_CONFIRMED = "confirmed"
STATUS_EXPIRED = "expired"

RULE_DIFFERENT_SENSOR = "different_sensor"
RULE_DIFFERENT_AREA = "different_area"
RULE_SAME_AREA = "same_area"
CONFIRMATION_RULES = [RULE_DIFFERENT_SENSOR, RULE_DIFFERENT_AREA, RULE_SAME_AREA]

ATTR_CONFIRMATION_ENABLED = "confirmation_enabled"
ATTR_CONFIRMATION_TIME = "confirmation_time"
ATTR_CONFIRMATION_RULE = "confirmation_rule"
ATTR_CONFIRMATION_REQUIRE_RESET = "confirmation_require_reset"
ATTR_ENTRY_ROUTE = "entry_route"
ATTR_HOLD_UP_CONFIRMATION_ENABLED = "hold_up_confirmation_enabled"
ATTR_HOLD_UP_CONFIRMATION_TIME = "hold_up_confirmation_time"
ATTR_HOLD_UP_TAMPER_CONFIRMS = "hold_up_tamper_confirms"

# defaults derived from the standards, to be verified against current editions
DEFAULT_CONFIRMATION_TIME = 1800
DEFAULT_HOLD_UP_CONFIRMATION_TIME = 28800


def resolve_area_and_device(hass: HomeAssistant, entity_id: str):
    """Return the HA area and device of an entity (area falls back to device)."""
    entry = er.async_get(hass).async_get(entity_id)
    if not entry:
        return (None, None)
    area_id = entry.area_id
    if not area_id and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        area_id = device.area_id if device else None
    return (area_id, entry.device_id)


def is_tamper(hass: HomeAssistant, entity_id: str, sensor_type: str) -> bool:
    """Return whether a sensor is a tamper sensor (by type or device class)."""
    state = hass.states.get(entity_id)
    return sensor_type == SENSOR_TYPE_TAMPER or (
        state is not None and state.attributes.get("device_class") == "tamper"
    )


def pair_satisfies_rule(rule: str, first: dict, second: dict) -> bool:
    """Check whether two tripped sensors independently confirm each other."""
    if first["entity_id"] == second["entity_id"]:
        return False
    if first["device_id"] and first["device_id"] == second["device_id"]:
        # several entities of one physical device are not independent
        return False
    if rule == RULE_SAME_AREA:
        return bool(first["ha_area_id"]) and first["ha_area_id"] == second["ha_area_id"]
    if rule == RULE_DIFFERENT_AREA:
        # sensors without an area are treated as for rule different_sensor
        return not (first["ha_area_id"] and first["ha_area_id"] == second["ha_area_id"])
    return True


class ConfirmationTracker:
    """Track whether an intruder alarm of an area is confirmed by a second sensor."""

    ENABLED_KEY = ATTR_CONFIRMATION_ENABLED
    TIME_KEY = ATTR_CONFIRMATION_TIME
    DEFAULT_TIME = DEFAULT_CONFIRMATION_TIME
    EVENTS = (
        const.EVENT_UNCONFIRMED,
        const.EVENT_CONFIRMED,
        const.EVENT_CONFIRMATION_EXPIRED,
    )
    ATTRIBUTE_KEYS = ("confirmation_status", "confirmed_by", "confirmation_expires")

    def __init__(self, hass: HomeAssistant, alarm_entity) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self._alarm_entity = alarm_entity
        self.status = STATUS_IDLE
        self.confirmed_by = []
        self.expires = None
        self._trips = {}
        self._entry_started = False
        self._timer = None

    @property
    def _config(self) -> dict:
        return self._alarm_entity._config

    @property
    def enabled(self) -> bool:
        """Return whether confirmation is enabled for the area."""
        return bool(self._config.get(self.ENABLED_KEY))

    @property
    def rule(self) -> str:
        """Return the configured confirmation rule."""
        return self._config.get(ATTR_CONFIRMATION_RULE) or RULE_DIFFERENT_SENSOR

    @property
    def attributes(self) -> dict:
        """Return the entity attributes describing the confirmation status."""
        (status, confirmed_by, expires) = self.ATTRIBUTE_KEYS
        return {
            status: self.status,
            confirmed_by: self.confirmed_by,
            expires: self.expires.isoformat() if self.expires else None,
            "reset_required": self._alarm_entity.reset_required,
        }

    @callback
    def record_trip(self, args: dict, sensor_config: dict) -> None:
        """Record a tripped sensor (from the sensor_tripped event)."""
        entity_id = args["sensor"]
        alarm = self._alarm_entity
        if (
            not self.enabled
            or self.status == STATUS_CONFIRMED
            or args["state"] != STATE_OPEN
            or not alarm.arm_mode
            or alarm.state
            not in (AlarmControlPanelState.PENDING, AlarmControlPanelState.TRIGGERED)
            or args["sensor_type"] in (SENSOR_TYPE_ENVIRONMENTAL, SENSOR_TYPE_HOLD_UP)
            or is_tamper(self.hass, entity_id, args["sensor_type"])
        ):
            return

        starts_entry = (
            alarm.state == AlarmControlPanelState.PENDING
            and args["alarm_state"] in const.ARM_MODES
        )
        skip = sensor_config.get(ATTR_ENTRY_ROUTE) and (
            self._entry_started or (starts_entry and self._trips)
        )
        if alarm.state == AlarmControlPanelState.PENDING:
            self._entry_started = True
        if skip:
            _LOGGER.debug("Ignoring entry route sensor %s for confirmation", entity_id)
            return

        self._add_trip(entity_id)
        self.async_evaluate()

    def _add_trip(self, entity_id: str) -> dict:
        if entity_id not in self._trips:
            self._trips[entity_id] = self._describe(entity_id)
        return self._trips[entity_id]

    def _describe(self, entity_id: str) -> dict:
        (ha_area_id, device_id) = resolve_area_and_device(self.hass, entity_id)
        if not ha_area_id:
            _LOGGER.debug("Sensor %s has no area for confirmation", entity_id)
        return {
            "entity_id": entity_id,
            "ha_area_id": ha_area_id,
            "device_id": device_id,
        }

    @callback
    def async_evaluate(self) -> None:
        """Open the window once the alarm is triggered, then look for a pair."""
        if not self._trips or self.status == STATUS_CONFIRMED:
            return
        if self.status == STATUS_IDLE:
            if self._alarm_entity.state != AlarmControlPanelState.TRIGGERED:
                return
            self._open_window()

        for first, second in itertools.combinations(self._trips.values(), 2):
            if pair_satisfies_rule(self.rule, first, second):
                self._confirm([first, second])
                return

    def _open_window(self) -> None:
        delay = int(self._config.get(self.TIME_KEY) or self.DEFAULT_TIME)
        self.status = STATUS_UNCONFIRMED
        self.expires = (dt_util.utcnow() + datetime.timedelta(seconds=delay)).replace(
            microsecond=0
        )
        self._timer = async_track_point_in_time(
            self.hass, self._async_window_expired, self.expires
        )
        self._fire(self.EVENTS[0], list(self._trips.values()))

    def _confirm(self, pair: list[dict]) -> None:
        self.cancel_timer()
        self.expires = None
        self._set_confirmed(pair)
        if self._alarm_entity.state != AlarmControlPanelState.TRIGGERED:
            self._alarm_entity.async_trigger(
                entry_delay=0, open_sensors=dict.fromkeys(self.confirmed_by, STATE_OPEN)
            )

    def _set_confirmed(self, pair: list[dict]) -> None:
        self.status = STATUS_CONFIRMED
        self.confirmed_by = [el["entity_id"] for el in pair]
        if self._config.get(ATTR_CONFIRMATION_REQUIRE_RESET):
            self._alarm_entity.reset_required = True
        _LOGGER.info("Alarm '%s' is confirmed (%s)", self._alarm_entity.name, pair)
        self._fire(self.EVENTS[1], pair)

    @callback
    def _async_window_expired(self, _now) -> None:
        self._timer = None
        self.expires = None
        if self.status == STATUS_UNCONFIRMED:
            self.status = STATUS_EXPIRED
            self._fire(self.EVENTS[2], list(self._trips.values()))
        self.reset()

    def _fire(self, event: str, sensors: list[dict]) -> None:
        self._alarm_entity.schedule_update_ha_state()
        dispatcher_send(
            self.hass,
            "alarmo_event",
            event,
            self._alarm_entity.area_id,
            {"rule": self.rule, "sensors": sensors},
        )

    def cancel_timer(self) -> None:
        """Cancel the window timer."""
        if self._timer:
            self._timer()
            self._timer = None

    @callback
    def reset(self, force: bool = False) -> None:
        """Return to idle, discarding any tripped sensors.

        A confirmed alarm awaiting a reset keeps its status, unless forced.
        """
        self.cancel_timer()
        if force or not (
            self.status == STATUS_CONFIRMED and self._alarm_entity.reset_required
        ):
            self.status = STATUS_IDLE
            self.confirmed_by = []
        self.expires = None
        self._trips = {}
        self._entry_started = False
        self._alarm_entity.schedule_update_ha_state()


class HoldUpTracker(ConfirmationTracker):
    """Track whether a hold-up (panic) alarm is confirmed, in any arm state."""

    ENABLED_KEY = ATTR_HOLD_UP_CONFIRMATION_ENABLED
    TIME_KEY = ATTR_HOLD_UP_CONFIRMATION_TIME
    DEFAULT_TIME = DEFAULT_HOLD_UP_CONFIRMATION_TIME
    EVENTS = (
        const.EVENT_HOLD_UP,
        const.EVENT_HOLD_UP_CONFIRMED,
        const.EVENT_HOLD_UP_EXPIRED,
    )
    ATTRIBUTE_KEYS = ("hold_up_status", "hold_up_confirmed_by", "hold_up_expires")

    @property
    def rule(self) -> str:
        """Hold-up devices only need to be independent of each other."""
        return RULE_DIFFERENT_SENSOR

    @callback
    def record_trip(self, args: dict, sensor_config: dict) -> None:
        """Record a hold-up activation, or a tamper signal confirming it."""
        entity_id = args["sensor"]
        if (
            not self.enabled
            or self.status == STATUS_CONFIRMED
            or args["state"] != STATE_OPEN
        ):
            return
        if args["sensor_type"] == SENSOR_TYPE_HOLD_UP:
            self._add_trip(entity_id)
            self.async_evaluate()
        elif (
            self.status == STATUS_UNCONFIRMED
            and self._config.get(ATTR_HOLD_UP_TAMPER_CONFIRMS)
            and is_tamper(self.hass, entity_id, args["sensor_type"])
        ):
            first = next(iter(self._trips.values()))
            self._set_confirmed([first, self._describe(entity_id)])

    @callback
    def async_evaluate(self) -> None:
        """Open the window on the first activation, then look for a pair."""
        if not self._trips or self.status == STATUS_CONFIRMED:
            return
        if self.status == STATUS_IDLE:
            self._open_window()
        for first, second in itertools.combinations(self._trips.values(), 2):
            if pair_satisfies_rule(self.rule, first, second):
                # the window keeps running, at its end the status returns to idle
                self._set_confirmed([first, second])
                return
