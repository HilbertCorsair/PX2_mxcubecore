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

"""PX2 flux estimate at the sample [ph/s].

flux = table(E) * I_ring / I_ref * aperture * attenuators * transmission / 100

The tabulated flux is measured at ``reference_current`` with the slits open,
no filters and the largest aperture. The live inputs come from the other
beamline objects, so the value follows energy, ring current, transmission,
aperture and filters instead of being a constant.
"""

import logging
import pickle

import gevent
from scipy.interpolate import interp1d

from mxcubecore import HardwareRepository as HWR
from mxcubecore.HardwareObjects.abstract.AbstractFlux import AbstractFlux

try:
    from attenuators import attenuators
except ModuleNotFoundError:
    try:
        from experimental_methods.attenuators import attenuators
    except ModuleNotFoundError:
        attenuators = None

DEFAULT_APERTURE_TRANSMISSION = {
    0: 0.95,
    1: 0.822,
    2: 0.287,
    3: 0.134,
    4: 0.081,
    5: 1.0,
}


class PX2Flux(AbstractFlux):
    def __init__(self, name):
        super().__init__(name)
        self.flux_as_f_of_energy = None
        self.reference_current = 500.0
        self.aperture_transmission = dict(DEFAULT_APERTURE_TRANSMISSION)
        self.attenuators = None
        self.refresh_interval = 5.0
        self._connected = set()

    def init(self):
        super().init()
        self.read_only = True
        self.reference_current = float(self.get_property("reference_current", 500.0))
        self.refresh_interval = float(self.get_property("refresh_interval", 5.0))
        apertures = self.get_property("aperture_transmission")
        if apertures:
            self.aperture_transmission = {int(k): float(v) for k, v in apertures.items()}

        table_path = self.get_property("flux_table")
        try:
            with open(table_path, "rb") as table_file:
                table = pickle.load(table_file, encoding="bytes")
            self.flux_as_f_of_energy = interp1d(
                table[:, 0], table[:, 1], bounds_error=False, fill_value="extrapolate"
            )
        except Exception:
            logging.getLogger("HWR").exception(
                "PX2Flux: cannot load flux table %s", table_path
            )

        if attenuators is not None:
            try:
                self.attenuators = attenuators()
            except Exception:
                logging.getLogger("HWR").exception("PX2Flux: no attenuators")

        self._connect_sources()
        self.update_state(self.STATES.READY)
        self.update_value()
        # Aperture and filters have no change signal here
        gevent.spawn(self._refresh_loop)

    def _connect_sources(self):
        # energy is loaded after flux in beamline.yaml, so retry until it exists
        for role in ("energy", "transmission", "machine_info"):
            source = getattr(HWR.beamline, role, None)
            if role not in self._connected and source is not None:
                self.connect(source, "valueChanged", self._input_changed)
                self._connected.add(role)

    def _refresh_loop(self):
        while True:
            gevent.sleep(self.refresh_interval)
            try:
                self._connect_sources()
                self.update_value()
            except Exception:
                logging.getLogger("HWR").exception("PX2Flux: refresh failed")

    def _input_changed(self, *_args):
        self.update_value()

    def _aperture_factor(self):
        diffractometer = HWR.beamline.diffractometer
        index = None
        if hasattr(diffractometer, "get_aperture_index"):
            index = diffractometer.get_aperture_index()
        return self.aperture_transmission.get(index, 1.0)

    def _attenuators_factor(self):
        if self.attenuators is None:
            return 1.0
        try:
            return float(self.attenuators.get_transmission())
        except Exception:
            return 1.0

    def get_value(self):
        """Flux [ph/s], or None when an input cannot be read."""
        if self.flux_as_f_of_energy is None:
            return None
        try:
            energy = HWR.beamline.energy.get_value()
            current = HWR.beamline.machine_info.get_current()
            transmission = HWR.beamline.transmission.get_value()
        except Exception:
            return None
        if energy is None or current is None or transmission is None:
            return None
        energy_ev = energy * 1e3 if energy < 1e3 else energy
        flux = (
            float(self.flux_as_f_of_energy(energy_ev))
            * float(current)
            / self.reference_current
            * self._aperture_factor()
            * self._attenuators_factor()
            * float(transmission)
            / 100.0
        )
        return max(flux, 0.0)

    def is_beam(self):
        """True when a non-zero flux is expected at the sample."""
        return bool(self.get_value())
