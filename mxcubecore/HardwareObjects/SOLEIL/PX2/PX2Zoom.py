"""SOLEIL Proxima 2A camera-zoom NState.

The MD2 zoom is a discrete predefined-position device (LEVEL1..LEVEL7).
Zoom level, focus offset and camera gain are all owned by the SOLEIL
``camera`` helper, so this class is a thin NState wrapper around it
rather than a motor.
"""

import logging

import gevent

from mxcubecore.BaseHardwareObjects import HardwareObjectState
from mxcubecore.HardwareObjects.mockup.MicrodiffZoomMockup import MicrodiffZoomMockup

try:
    from camera import camera
except ModuleNotFoundError:
    from experimental_methods import camera


class PX2Zoom(MicrodiffZoomMockup):

    def __init__(self, name):
        super().__init__(name)
        self.camera = None

    def init(self):
        self.camera = camera(use_redis=True)
        super().init()
        try:
            self.update_value(self.get_value())
        except Exception:
            logging.getLogger("HWR").exception("PX2Zoom: initial zoom read failed")
        self.update_state(HardwareObjectState.READY)

    def get_value(self):
        try:
            level = int(self.camera.get_zoom())
        except Exception:
            return self.VALUES.UNKNOWN
        return self.value_to_enum(level)

    def _set_value(self, value):
        gevent.spawn(self._set_zoom, value)

    def _set_zoom(self, value, adjust_zoom=True):
        level = value.value
        self.update_state(HardwareObjectState.BUSY)
        try:
            self.camera.set_zoom(level, adjust_zoom=adjust_zoom)
        finally:
            self._nominal_value = value
            self.update_value(value)
            focus_offset = self.camera.focus_offsets.get(level)
            self.emit("predefinedPositionChanged", (level, focus_offset))
            self.update_state(HardwareObjectState.READY)
