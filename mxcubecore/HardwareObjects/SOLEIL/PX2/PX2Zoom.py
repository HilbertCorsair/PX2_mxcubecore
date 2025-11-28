import logging
import math
import gevent
from mxcubecore.HardwareObjects.mockup.MicrodiffZoomMockup import MicrodiffZoomMockup
from mxcubecore.HardwareObjects.SOLEIL.SOLEILMicrodiffMotor import SOLEILMicrodiffMotor

try:
    from camera import camera
except ModuleNotFoundError:
    from experimental_methods import camera
    

class PX2Zoom(SOLEILMicrodiffMotor, MicrodiffZoomMockup):
    def __init__(self, name):
        SOLEILMicrodiffMotor.__init__(self, name)
        MicrodiffZoomMockup.__init__(self, name)
        self.camera = camera(use_redis=True)
        self.predefined_position_channel = None

    def init(self):
        SOLEILMicrodiffMotor.init(self)
        MicrodiffZoomMockup.init(self)
        # self.predefined_position_channel = self.get_channel_object("predefined_position")

    def get_limits(self):
        return (1, 7)

    def _set_value(self, value):
        """Overrriden from AbstractActuator"""
        gevent.spawn(self._set_zoom, value)

    def _set_zoom(self, value, adjust_zoom=True, sleeptime=0.1):
        logging.info("value %s value.value %s" % (value, value.value))
        self._nominal_value = value
        self.camera.set_zoom(value.value, adjust_zoom=adjust_zoom)
        self.emit("valueChanged", value.value)
        self.emit(
            "predefinedPositionChanged",
            (value.value, self.camera.focus_offsets[value.value]),
        )

    def get_value(self):
        zoom = int(self.camera.get_zoom())
        return zoom
