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

"""PX2 transmission defined by the secondary slits (fent_h.2 / fent_v.2).

Transmission is the fraction of the measured reference beam distribution that
passes through the current slit opening, computed in process from the slit
gap/position Tango channels. This replaces the px2em ``transmission`` helper,
which needed its zmq broker to be running and read 0 when it was not.
"""

import logging

import gevent
import numpy as np
from scipy.interpolate import interp1d

from mxcubecore.HardwareObjects.abstract.AbstractTransmission import (
    AbstractTransmission,
)

READY_STATES = ("STANDBY", "ON", "ALARM")
BUSY_STATES = ("MOVING", "RUNNING", "INIT")


class SOLEILTransmission(AbstractTransmission):
    def __init__(self, name):
        super().__init__(name)
        self.distribution = None
        self.reference_gap = 4.0
        self.npixels = 4000
        self.extent = (-2.0, 2.0)
        self.percent_factor = 100.0
        self._gap_from_transmission = None
        self._slit_states = {}

    def init(self):
        super().init()
        self.reference_gap = float(self.get_property("reference_gap", 4.0))

        path = self.get_property("reference_distribution")
        try:
            reference = np.load(path)
        except Exception:
            logging.getLogger("HWR").exception(
                "SOLEILTransmission: cannot load reference distribution %s", path
            )
        else:
            # Summed-area table, zero-padded on the leading edges, so the
            # transmission of any slit opening is four lookups
            self.npixels = reference.shape[0]
            sat = np.zeros((self.npixels + 1, reference.shape[1] + 1))
            np.cumsum(reference, axis=0, out=sat[1:, 1:])
            np.cumsum(sat[1:, 1:], axis=1, out=sat[1:, 1:])
            self.distribution = sat
            gevent.spawn(self._build_inverse)

        for name in ("h_gap", "v_gap", "h_position", "v_position"):
            self.get_channel_object(name).connect_signal("update", self.value_changed)
        for name in ("h_state", "v_state"):
            chan = self.get_channel_object(name)
            chan.connect_signal(
                "update", lambda state, name=name: self.state_changed(name, state)
            )
            self.state_changed(name, chan.get_value())

        self.update_value()

    def _integrate(self, h_gap, v_gap, h_pos, v_pos):
        """Fraction [%] of the reference beam inside the given slit opening."""
        pixels_per_mm = self.npixels / (self.extent[1] - self.extent[0])

        def edges(gap, pos):
            start = (-gap / 2.0 - pos - self.extent[0]) * pixels_per_mm
            end = (gap / 2.0 - pos - self.extent[0]) * pixels_per_mm
            start = int(min(max(start, 0), self.npixels - 1))
            end = int(min(max(end, 0), self.npixels - 1))
            return start, end

        v0, v1 = edges(v_gap, v_pos)
        h0, h1 = edges(h_gap, h_pos)
        if v1 < v0 or h1 < h0:
            return 0.0
        sat = self.distribution
        total = sat[v1 + 1, h1 + 1] - sat[v0, h1 + 1] - sat[v1 + 1, h0] + sat[v0, h0]
        return float(abs(total) * self.percent_factor)

    def _build_inverse(self):
        gaps = np.linspace(0, self.reference_gap, 400)
        transmissions = [self._integrate(gap, gap, 0.0, 0.0) for gap in gaps]
        self._gap_from_transmission = interp1d(
            transmissions,
            gaps,
            bounds_error=False,
            fill_value=(0.0, self.reference_gap),
        )

    def state_changed(self, name, state):
        state = getattr(state, "name", str(state)).upper()
        self._slit_states[name] = state
        states = self._slit_states.values()
        if any(s in BUSY_STATES for s in states):
            self.update_state(self.STATES.BUSY)
        elif all(s in READY_STATES for s in states):
            self.update_state(self.STATES.READY)
        else:
            self.update_state(self.STATES.FAULT)

    def value_changed(self, _value=None):
        self.update_value()

    def get_value(self):
        """Transmission [%], or None when the slits cannot be read."""
        if self.distribution is None:
            return None
        try:
            values = [
                float(self.get_channel_object(name).get_value())
                for name in ("h_gap", "v_gap", "h_position", "v_position")
            ]
        except Exception:
            return None
        return self._integrate(*values)

    def _set_value(self, value):
        if self._gap_from_transmission is None:
            raise RuntimeError("Transmission calibration not loaded yet")
        gap = float(self._gap_from_transmission(min(max(value, 0.0), 100.0)))
        self.get_channel_object("h_gap").set_value(gap)
        self.get_channel_object("v_gap").set_value(gap)
