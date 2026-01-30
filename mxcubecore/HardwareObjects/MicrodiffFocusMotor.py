from mxcubecore.HardwareObjects.abstract.AbstractMotor import AbstractMotor
from mxcubecore import HardwareRepository as HWR


class MicrodiffFocusMotor(AbstractMotor):
    def __init__(self, name):
        AbstractMotor.__init__(self, name)

    def init(self):

        if HWR.beamline.diffractometer.in_plate_mode():
            self.actuator_name = self.get_property("centring_focus")
        else:
            self.actuator_name = self.get_property("alignment_focus")
        AbstractMotor.init(self)
