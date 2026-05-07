"""SOLEIL CATS maintenance proxy.

Thin pass-through to ``HWR.beamline.sample_changer`` (a ``SOLEILCats``
instance). The web adapter expects a separate ``sample_changer_maintenance``
object exposing ``get_cmd_info``, ``get_global_state``, ``send_command`` and
emitting ``globalStateChanged`` / ``gripperChanged``. Rather than maintaining
a second class with its own Tango channels (which is what the legacy
``SOLEILCatsMaint`` did, and what ``CatsMaintMockup`` was masquerading as in
production), this proxy forwards everything to the live SOLEILCats instance
and re-emits the ``globalStateChanged`` signal it produces.
"""

import logging

from mxcubecore import HardwareRepository as HWR
from mxcubecore.BaseHardwareObjects import HardwareObject


class SOLEILCatsMaint(HardwareObject):
    __TYPE__ = "CATS"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._connected = False

    def init(self):
        self._connect_to_sample_changer()

    def _connect_to_sample_changer(self):
        sc = self._sc()
        if sc is None or self._connected:
            return
        sc.connect("globalStateChanged", self._forward_global_state)
        sc.connect("connectionStateChanged", self._forward_connection_state)
        self._connected = True

    @staticmethod
    def _sc():
        return HWR.beamline.sample_changer

    def _forward_global_state(self, *args):
        self.emit("globalStateChanged", args)

    def _forward_connection_state(self, *args):
        self.emit("connectionStateChanged", args)

    def check_connection(self):
        sc = self._sc()
        if sc is None:
            return False, "Sample changer not loaded"
        return sc.check_connection()

    # ------------------------------------------------------------------
    # Web-adapter API
    # ------------------------------------------------------------------

    def get_cmd_info(self):
        sc = self._sc()
        if sc is None:
            return []
        return sc.get_cmd_info()

    def get_global_state(self):
        sc = self._sc()
        if sc is None:
            return {}, "Sample changer not loaded", ""
        return sc.get_global_state()

    def send_command(self, cmd_name, args=None):
        sc = self._sc()
        if sc is None:
            raise Exception("Sample changer not loaded")
        # First call may happen before init has had a chance to connect;
        # ensure the signal forwarding is wired up.
        if not self._connected:
            self._connect_to_sample_changer()
        try:
            return sc.send_command(cmd_name, args)
        except Exception:
            logging.getLogger("HWR").exception(
                "SOLEILCatsMaint: send_command(%s) failed", cmd_name
            )
            raise
