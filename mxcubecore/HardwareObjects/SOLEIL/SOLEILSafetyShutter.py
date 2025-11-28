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
#  You should have received a copy of the GNU Lesser General Public License
#  along with MXCuBE. If not, see <http://www.gnu.org/licenses/>.

from mxcubecore.BaseHardwareObjects import HardwareObject

import logging
import traceback

class SOLEILSafetyShutter(HardwareObject):
    def __init__(self, name):

        HardwareObject.__init__(self, name)

        self.pss = None
        self.shutter = None

    def init(self):
        try:
            self.shutter = self.get_object_by_role("shutter")
            self.pss = self.get_object_by_role("pss")
            logging.info("shutter is " + str(self.shutter))
            logging.info("pss is " + str(self.pss))
            self.connect(self.shutter, "shutterStateChanged", self.shutterStateChanged)
            self.connect(self.pss, "wagoStateChanged", self.shutterStateChanged)
        except Exception:
            print(traceback.print_exc())
            logging.warning(traceback.format_exc())
            logging.getLogger().warning("pss device not configured")

    def getShutterState(self):
        logging.debug(" shutter is %s " % str(self.shutter))

        #if self.pss.getWagoState() != "ready":
            #return "disabled"

        if self.shutter is None:
            return "unknown"
        return self.shutter.getShutterState()

    def shutterStateChanged(self, value):
        if self.shutter is None:
            return
        #
        # emit signal
        #
        self.shutterStateValue = value  # str(value)
        self.emit("shutterStateChanged", (self.getShutterState(),))
        self.emit("stateChanged", (self.getShutterState(),))

    def force_emit_signals(self):
        self.emit("shutterStateChanged", (self.getShutterState(),))
        self.emit("stateChanged", (self.getShutterState(),))
        
    def open(self):
        if self.shutter is None:
            return
        if self.pss is None:
            logging.error("no pss device for safety shutter. check configuration")
            return

        if self.pss.getWagoState() == "ready":
            self.shutter.openShutter()
            logging.info("Opening shutter ok")
        else:
            logging.warning("cannot open safety shutter. Check interlock")

    def close(self):
        if self.shutter is None:
            return
        self.shutter.closeShutter()

    def is_open(self):
        return self.getShutterState().lower() == 'open'
    
    def is_closed(self):
        return not self.is_open()
    
