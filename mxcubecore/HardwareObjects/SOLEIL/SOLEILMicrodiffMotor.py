"""SOLEIL Proxima MD2 motor with HardwareObjectState reporting.

Translates raw motor state strings (Exporter ``"Ready"``, ``"Moving"``, ...
or Tango ``DevState`` enum values) into the canonical
``HardwareObjectState`` enum, replacing the broken legacy bookkeeping
inherited from ``MicrodiffMotor`` whose ``__init__`` references a
non-existent ``self.motor_states`` enum.
"""

from gevent import spawn

from mxcubecore.BaseHardwareObjects import HardwareObjectState
from mxcubecore.HardwareObjects.abstract.AbstractMotor import AbstractMotor
from mxcubecore.HardwareObjects.MicrodiffMotor import MicrodiffMotor

try:
    from goniometer import goniometer
except ModuleNotFoundError:
    from experimental_methods import goniometer


class SOLEILMicrodiffMotor(MicrodiffMotor):

    EXPORTER_TO_HWSTATE = {
        "Ready": HardwareObjectState.READY,
        "Standby": HardwareObjectState.READY,
        "STANDBY": HardwareObjectState.READY,
        "LowLim": HardwareObjectState.READY,
        "HighLim": HardwareObjectState.READY,
        "Moving": HardwareObjectState.BUSY,
        "MOVING": HardwareObjectState.BUSY,
        "Initializing": HardwareObjectState.BUSY,
        "Created": HardwareObjectState.UNKNOWN,
        "Unknown": HardwareObjectState.UNKNOWN,
        "Invalid": HardwareObjectState.FAULT,
        "Fault": HardwareObjectState.FAULT,
        "Offline": HardwareObjectState.OFF,
    }

    def __init__(self, name):
        # Skip MicrodiffMotor.__init__ — it builds a translate_state dict
        # from a non-existent ``self.motor_states`` enum and raises
        # AttributeError. We replicate only the two suffix attributes
        # MicrodiffMotor.init() actually relies on.
        AbstractMotor.__init__(self, name)
        self.motor_pos_attr_suffix = "Position"
        self.motor_state_attr_suffix = "State"
        self.goniometer = goniometer()

    def init(self):
        super().init()
        self.update_state(self.get_state())

    def _motor_state_to_hwstate(self, raw):
        """Map a raw motor state to ``HardwareObjectState``.

        Accepts MD2 Exporter strings (``"Ready"``, ``"Moving"`` ...) and
        Tango ``DevState`` enum values (which expose ``.name``).
        """
        if raw is None:
            return HardwareObjectState.UNKNOWN
        if hasattr(raw, "name"):
            raw = raw.name
        return self.EXPORTER_TO_HWSTATE.get(str(raw), HardwareObjectState.UNKNOWN)

    def _get_state(self):
        return self._motor_state_to_hwstate(self.state_attr.get_value())

    def get_state(self):
        return self._get_state()

    def updateMotorState(self, motor_states):
        parsed = dict(item.split("=") for item in motor_states)
        new_state = self._motor_state_to_hwstate(
            parsed.get(self.actuator_name, "Ready")
        )
        if self.specific_state == new_state:
            return
        self.update_state(new_state)

    def motorStateChanged(self, state):
        hw_state = (
            state
            if isinstance(state, HardwareObjectState)
            else self._motor_state_to_hwstate(state)
        )
        self.update_state(hw_state)
        self.emit("stateChanged", (hw_state,))

    def motorIsMoving(self):
        return self._get_state() == HardwareObjectState.BUSY

    def stop(self):
        if self._get_state() != HardwareObjectState.UNKNOWN:
            self._motor_abort()

    def _set_value(self, position, wait=True, timeout=None):
        if abs(self.get_value() - position) < self.motor_resolution:
            return
        actuator = self.actuator_name.lower()
        if actuator == "kappa":
            spawn(self.goniometer.set_kappa_position, position)
        elif actuator == "phi":
            spawn(self.goniometer.set_phi_position, position)
        elif hasattr(self.goniometer, f"set_{actuator}_position"):
            spawn(getattr(self.goniometer, f"set_{actuator}_position"), position)
        else:
            self.position_attr.set_value(position)
