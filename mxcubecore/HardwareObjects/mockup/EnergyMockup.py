#
#  Project: MXCuBE
#  https://github.com/mxcube
#
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
#  You should have received a copy of the GNU General Lesser Public License
#  along with MXCuBE. If not, see <http://www.gnu.org/licenses/>.

"""Mockup class for testing purposes"""

import time

from mxcubecore.HardwareObjects.abstract.AbstractEnergy import AbstractEnergy
from mxcubecore.HardwareObjects.mockup.ActuatorMockup import ActuatorMockup

# Default energy value (keV)
# 13.145 determined by Bill and Serena during start-up
# 13.234 determined by Bill on Saturday 2024-12-08 
# 13.2045 determined by Martin on Tuesday 2024-12-10 from LaB6 rings using pyFAI-calib2 on a single image
# 13.2150 determined by Martin on Thursday 2024-12-12 from LaB6 rings using pyFAI on 394 images collected in multiple series, at two different transmissions over 17 different distances between 110 and 250 mm
DEFAULT_VALUE = 12.650 #13.1792 #15.3556 #15.306 #15.3485 #15.3700 #13.2150
# Default energy limits (keV)
DEFAULT_LIMITS = (4, 20)


class EnergyMockup(ActuatorMockup, AbstractEnergy):
    """Energy Mockup class"""

    def init(self):
        """Initialise default properties"""
        super(EnergyMockup, self).init()

        if None in self.get_limits():
            self.update_limits(DEFAULT_LIMITS)
        if self.default_value is None:
            self.default_value = DEFAULT_VALUE
            self.update_value(DEFAULT_VALUE)
        self.update_state(self.STATES.READY)

    def get_limits(self):
        my_limits = ActuatorMockup.get_limits(self)
        return my_limits
    def _move(self, value):
        """ Simulated energy change
        Args:
            value (float): target energy
        """
        start_pos = self.get_value()
        if value is not None and start_pos is not None:
            step = -1 if value < start_pos else 1
            for _val in range(int(start_pos) + step, int(value) + step, step):
                time.sleep(0.2)
                self.update_value(_val)
        time.sleep(0.2)
        return value
