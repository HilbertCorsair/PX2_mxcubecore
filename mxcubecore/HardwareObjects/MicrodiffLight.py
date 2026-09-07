"""MicrodiffLight: light intensity + on/off backed by two MD2 attributes.

Single hardware object per light (back or front). It exposes the level
as an ``AbstractMotor`` (limits / value / state) and the on/off as a
discrete switch via ``is_on`` / ``set_on`` / ``set_off`` / ``set_switch``.

YAML configuration declares the two channels:

```yaml
class: mxcubecore.HardwareObjects.MicrodiffLight.MicrodiffLight
configuration:
  username: BackLight
  limits: "0,100"
tango:
  i11-ma-cx1/ex/md2:
    channels:
      chanLightValue:
        attribute: BackLightLevel
        polling_period: 1000
      chanLightIsOn:
        attribute: BackLightIsOn
        polling_period: 1000
```

State is pinned to READY because the light has no concept of "moving".
The dedicated ``LightAdapter`` (mxcubeweb side) wires both controls
into the single redux entry that ``LightControl.jsx`` reads.

The MD2 does not send events when an actuator changes (see the comment in
``ExporterNState._set_value``), and on this diffractometer ``FrontLightIsOn``
does not reliably go back to false after being switched off -- only
``FrontLightLevel`` does. The switch state is therefore derived from *both*
attributes, and every write re-reads and re-emits instead of relying on the
channel event to arrive.
"""

from enum import Enum, unique

import gevent

from mxcubecore.BaseHardwareObjects import HardwareObjectState
from mxcubecore.HardwareObjects.abstract.AbstractMotor import AbstractMotor

# How long to wait for a switch write to show up on the read-back, and how
# often to re-read while waiting. Kept short: the web adapter calls set_switch()
# synchronously from the Flask view.
SWITCH_READBACK_TIMEOUT = 1.0
SWITCH_READBACK_INTERVAL = 0.1

_TRUE_STRINGS = ("true", "1", "on", "yes")


@unique
class LightSwitchValues(Enum):
    IN = "IN"
    OUT = "OUT"


def _as_bool(value):
    """Coerce a channel value to a bool.

    Tango boolean attributes come back as real bools, but the exporter
    channels used for the same attributes at other sites return the raw
    string reply -- and ``bool("false")`` is ``True``.
    """
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_STRINGS
    return bool(value)


class MicrodiffLight(AbstractMotor):
    """Light intensity + on/off as one HardwareObject."""

    SWITCH_VALUES = LightSwitchValues

    def __init__(self, name):
        super().__init__(name)
        self.chan_value = None
        self.chan_is_on = None
        self._level_before_off = None
        self._last_switch_value = None

    def init(self):
        super().init()

        self.chan_value = self.get_channel_object("chanLightValue")
        if self.chan_value is not None:
            self.chan_value.connect_signal("update", self._on_value_update)

        self.chan_is_on = self.get_channel_object("chanLightIsOn")
        if self.chan_is_on is not None:
            self.chan_is_on.connect_signal("update", self._on_is_on_update)

        raw_limits = self.get_property("limits", "0,100")
        try:
            low, high = (float(x) for x in str(raw_limits).strip("[]()").split(","))
            self._nominal_limits = (low, high)
        except (TypeError, ValueError):
            self._nominal_limits = (0.0, 100.0)

        self.update_state(HardwareObjectState.READY)

    def _on_value_update(self, value):
        # The switch state is derived from the level too, so the on/off
        # button has to follow the intensity slider.
        self.update_value(value)
        self._emit_switch()

    def _on_is_on_update(self, _value):
        self._emit_switch()

    def _emit_switch(self, force=False):
        """Push the current switch state to whoever listens (the adapter).

        The level channel is polled once a second and the switch state is
        derived from it, so only emit on an actual change -- each signal makes
        the adapter push a full payload to every connected browser.
        """
        value = self.switch_value()
        if not force and value == self._last_switch_value:
            return

        self._last_switch_value = value
        self.emit("lightSwitchChanged", value)

    # -- AbstractMotor surface (slider) --

    def get_value(self):
        if self.chan_value is None:
            return self._nominal_value
        return self.chan_value.get_value()

    def _set_value(self, value):
        if self.chan_value is not None:
            self.chan_value.set_value(float(value))

    def get_limits(self):
        return self._nominal_limits

    def get_state(self):
        return HardwareObjectState.READY

    def abort(self):
        pass

    def force_emit_signals(self):
        super().force_emit_signals()
        self._emit_switch(force=True)

    # -- Switch surface (on/off button) --

    def _level(self):
        """Current light level, or ``None`` when it cannot be read."""
        try:
            return float(self.get_value())
        except (TypeError, ValueError):
            return None

    def _is_on_flag(self):
        if self.chan_is_on is None:
            return False
        return _as_bool(self.chan_is_on.get_value())

    def is_on(self):
        """Return whether the light actually illuminates.

        A light at level 0 is off whatever the on/off flag claims -- which is
        the case for the front light, whose ``FrontLightIsOn`` stays true after
        being switched off.
        """
        if not self._is_on_flag():
            return False

        level = self._level()
        return True if level is None else level > 0

    def _wait_switch(self, expected):
        """Re-read the switch until it matches ``expected`` or we give up."""
        waited = 0.0
        while waited < SWITCH_READBACK_TIMEOUT:
            if self.is_on() == expected:
                return True
            gevent.sleep(SWITCH_READBACK_INTERVAL)
            waited += SWITCH_READBACK_INTERVAL
        return self.is_on() == expected

    def set_on(self):
        level = self._level()
        if level is not None and level > 0:
            self._level_before_off = level

        if self.chan_is_on is not None:
            self.chan_is_on.set_value(True)

        if not self._wait_switch(True):
            # The MD2 did not restore the level by itself, put it back.
            self._restore_level()
            self._wait_switch(True)

        self._emit_switch()

    def set_off(self):
        level = self._level()
        if level is not None and level > 0:
            self._level_before_off = level

        if self.chan_is_on is not None:
            self.chan_is_on.set_value(False)

        if not self._wait_switch(False):
            # The MD2 kept the light lit (its "is on" flag and its level both
            # unchanged); take the level down ourselves. set_on() restores it.
            self._set_value(self._nominal_limits[0])
            self._wait_switch(False)

        self._emit_switch()

    def _restore_level(self):
        """Set the level back to what it was before the light was switched off."""
        level = self._level_before_off
        if not level:
            low, high = self._nominal_limits
            level = (low + high) / 2

        self._set_value(level)

    def switch_value(self):
        return (
            self.SWITCH_VALUES.IN.value
            if self.is_on()
            else self.SWITCH_VALUES.OUT.value
        )

    def switch_commands(self):
        return [v.value for v in self.SWITCH_VALUES]

    def set_switch(self, value):
        """Set the on/off state from an ``"IN"`` / ``"OUT"`` string."""
        if str(value).upper() == self.SWITCH_VALUES.IN.value:
            self.set_on()
        else:
            self.set_off()
