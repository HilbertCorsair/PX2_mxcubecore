#  This file is part of MXCuBE software.
#
#  MXCuBE is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  MXCuBE is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with MXCuBE. If not, see <http://www.gnu.org/licenses/>.

"""Read-only sample temperature from the cryostream Tango device.

Example yaml configuration:

.. code-block:: yaml

 class: mxcubecore.HardwareObjects.SOLEIL.SOLEILCryo.SOLEILCryo
 configuration:
   actuator_name: Cryo
 tango:
   "i11-ma-cx1/ex/cryostream800":
     channels:
       temperature:
         attribute: sampleTemp
         polling_period: 1000
"""

from mxcubecore.HardwareObjects.abstract.AbstractActuator import AbstractActuator

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


class SOLEILCryo(AbstractActuator):
    """Sample temperature [K], read only."""

    def __init__(self, name):
        super().__init__(name)
        self.read_only = True
        self.temperature_channel = None

    def init(self):
        super().init()
        self.read_only = True
        self.temperature_channel = self.get_channel_object("temperature")
        self.temperature_channel.connect_signal("update", self.temperature_changed)
        self.temperature_changed(self.get_value())

    def temperature_changed(self, value):
        if value is None:
            self.update_state(self.STATES.FAULT)
            return
        self.update_value(float(value))
        self.update_state(self.STATES.READY)

    def get_value(self):
        try:
            return float(self.temperature_channel.get_value())
        except Exception:
            return None

    def _set_value(self, value):
        raise ValueError("Sample temperature is read only")
