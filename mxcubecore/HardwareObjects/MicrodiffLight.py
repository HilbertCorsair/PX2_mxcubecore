"""MicrodiffLight: light intensity + on/off backed by two Tango attributes.

Single hardware object per light (back or front). It exposes the level
as an ``AbstractMotor`` (limits / value / state) and the on/off as a
discrete switch via ``is_on`` / ``set_on`` / ``set_off`` / ``set_switch``.

YAML configuration declares the two Tango channels:

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
"""

from enum import Enum, unique

from mxcubecore.BaseHardwareObjects import HardwareObjectState
from mxcubecore.HardwareObjects.abstract.AbstractMotor import AbstractMotor


@unique
class LightSwitchValues(Enum):
    IN = "IN"
    OUT = "OUT"


class MicrodiffLight(AbstractMotor):
    """Light intensity + on/off as one HardwareObject."""

    SWITCH_VALUES = LightSwitchValues

    def __init__(self, name):
        super().__init__(name)
        self.chan_value = None
        self.chan_is_on = None

    def init(self):
        super().init()

        self.chan_value = self.get_channel_object("chanLightValue")
        if self.chan_value is not None:
            self.chan_value.connect_signal("update", self.update_value)

        self.chan_is_on = self.get_channel_object("chanLightIsOn")
        if self.chan_is_on is not None:
            self.chan_is_on.connect_signal("update", self._on_is_on_update)

        raw_limits = self.get_property("limits", "0,100")
        try:
            low, high = (float(x) for x in str(raw_limits).split(","))
            self._nominal_limits = (low, high)
        except (TypeError, ValueError):
            self._nominal_limits = (0.0, 100.0)

        self.update_state(HardwareObjectState.READY)

    def _on_is_on_update(self, _value):
        # Re-emit value so the adapter pushes the new switch state.
        self.emit("lightSwitchChanged", self.switch_value())

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

    # -- Switch surface (on/off button) --

    def is_on(self):
        if self.chan_is_on is None:
            return False
        return bool(self.chan_is_on.get_value())

    def set_on(self):
        if self.chan_is_on is not None:
            self.chan_is_on.set_value(True)

    def set_off(self):
        if self.chan_is_on is not None:
            self.chan_is_on.set_value(False)

    def switch_value(self):
        return self.SWITCH_VALUES.IN.value if self.is_on() else self.SWITCH_VALUES.OUT.value

    def switch_commands(self):
        return [v.value for v in self.SWITCH_VALUES]

    def set_switch(self, value):
        """Set the on/off state from an ``"IN"`` / ``"OUT"`` string."""
        if str(value).upper() == self.SWITCH_VALUES.IN.value:
            self.set_on()
        else:
            self.set_off()
