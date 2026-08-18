#  Project: MXCuBE
#  https://github.com/mxcube.
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
#  along with MXCuBE.  If not, see <http://www.gnu.org/licenses/>.
"""PX2 beam object.

Defines the beam size/shape from the aperture, slits or definer (the same
behaviour as ``BeamMockup``, folded in here so PX2Beam derives directly from
``AbstractBeam``), and adds a *functional* hardware state: ``AbstractBeam`` has
no state of its own and never calls ``update_state()``, so the web UI beam-state
pill was stuck at UNKNOWN. Here ``get_state()`` mirrors the beamline safety
shutter and stays in sync as it changes, falling back to UNKNOWN only when the
shutter is missing or the read fails.
"""

import ast
import logging

from mxcubecore import HardwareRepository as HWR
from mxcubecore.HardwareObjects.abstract.AbstractBeam import AbstractBeam, BeamShape

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


class PX2Beam(AbstractBeam):
    """Beam defined by aperture/slits/definer, state mirroring the safety shutter."""

    def __init__(self, name):
        super().__init__(name)
        self._definer_type = None
        self._check_beam = ()
        self._safety_shutter = None
        self._shutter_connected = False

    def init(self):
        """Initialize hardware."""
        super().init()

        #
        # backward compatibility hack to support loading from XML config file
        #
        # when loading from YAML configuration file,
        # the attributes will be automatically set to the specified child HWOBJs
        #
        # when loading from XML, it does not happen, so fall back to
        # get_object_by_role()
        #
        _definer_type = None
        self._aperture = self.get_object_by_role("aperture")
        self._slits = self.get_object_by_role("slits")
        self._definer = self.get_object_by_role("definer")

        if self.aperture:
            _definer_type = "aperture"
            self.aperture.connect("valueChanged", self.aperture_diameter_changed)

        if self.slits:
            _definer_type = "slits"
            self.slits.connect("valueChanged", self.slits_gap_changed)

        if self.definer:
            _definer_type = "definer"
            self.definer.connect("valueChanged", self._re_emit_values)

        self._definer_type = self.get_property("definer_type", _definer_type)

        raw_position = self.get_property("beam_position")
        if isinstance(raw_position, str):
            raw_position = ast.literal_eval(raw_position)
        self._beam_position_on_screen = (
            tuple(raw_position) if raw_position else (318, 238)
        )

        self._check_beam = self.get_property("check_beam") or False

        # Needed to trigger first value setting
        self.get_value()

        self.re_emit_values()
        self.emit("beamPosChanged", (self._beam_position_on_screen,))

        # Publish an initial functional state from the safety shutter.
        self.update_state(self.get_state())

    # ------------------------------------------------------------------ #
    # Functional state: mirror the beamline safety shutter               #
    # ------------------------------------------------------------------ #
    def _get_safety_shutter(self):
        """Return the safety shutter, connecting its signals once available."""
        if self._safety_shutter is None:
            try:
                self._safety_shutter = HWR.beamline.safety_shutter
            except Exception:
                return None

        shutter = self._safety_shutter
        if shutter is not None and not self._shutter_connected:
            try:
                shutter.connect("stateChanged", self._shutter_state_changed)
                shutter.connect("valueChanged", self._shutter_state_changed)
                self._shutter_connected = True
            except Exception:
                logging.getLogger("HWR").exception(
                    "PX2Beam: could not connect safety-shutter signals"
                )
        return shutter

    def _shutter_state_changed(self, *args, **kwargs):
        """Propagate a shutter state/value change to the beam state pill."""
        self.update_state(self.get_state())

    def get_state(self):
        """Mirror the safety-shutter state; UNKNOWN only on missing/read error."""
        shutter = self._get_safety_shutter()
        if shutter is None:
            return self.STATES.UNKNOWN
        try:
            state = shutter.get_state()
        except Exception:
            return self.STATES.UNKNOWN
        return state if state is not None else self.STATES.UNKNOWN

    # ------------------------------------------------------------------ #
    # Beam definition (aperture / slits / definer)                       #
    # ------------------------------------------------------------------ #
    def _re_emit_values(self, *args, **kwargs):
        self.re_emit_values()

    def _get_aperture_value(self) -> tuple[list[float, float], str]:
        """Get the size and the label of the aperture in place.

        Returns:
            Size [mm] (width, height), label.
        """
        _size = self.aperture.get_value().value[0]
        try:
            _label = self.aperture.get_value().name
        except AttributeError:
            _label = str(_size)
        _size /= 1000.0

        return [_size, _size], _label

    def _get_definer_value(self) -> tuple[list[float, float], str]:
        """Get the size and the name of the definer in place.

        Returns:
            Size [mm] (width, height), label.
        """
        try:
            value = self.definer.get_value()
            if isinstance(value, tuple):
                return [value[1], value[1]], value[0]
            return list(value.value), value.name
        except AttributeError:
            return [-1, -1], "UNKNOWN"

    def _get_slits_value(self) -> tuple[list[float, float], str]:
        """Get the size of the slits in place.

        Returns:
             Size [mm] (width, height), label.
        """
        _size = self.slits.get_gaps()
        return _size, "slits"

    def _get_value(self) -> tuple[float, float, BeamShape, str]:
        """Get the size (width and height) of the beam, its shape and
        its label. The size is in mm.

        Returns:
            Four-item tuple: width, height, shape, name
        """
        labels = {}
        _label = "UNKNOWN"
        if self.aperture:
            _size, _name = self._get_aperture_value()
            self._beam_size_dict.update({"aperture": _size})
            labels.update({"aperture": _name})

        if self.slits:
            _size, _name = self._get_slits_value()
            self._beam_size_dict.update({"slits": _size})
            labels.update({"slits": _name})

        if self.definer:
            _size, _name = self._get_definer_value()
            self._beam_size_dict.update({"definer": _size})
            labels.update({"definer": _name})

        info_dict = self.evaluate_beam_info()

        try:
            _label = labels[info_dict["label"]]
            self._beam_info_dict["label"] = _label
        except KeyError:
            _label = info_dict["label"]

        return self._beam_width, self._beam_height, self._beam_shape, _label

    def aperture_diameter_changed(self, aperture):
        """Method called when the aperture diameter changes.

        Args:
            Aperture enum.
        """
        size = aperture.value[0]
        self.aperture.update_value(aperture)
        self._beam_size_dict["aperture"] = [size, size]
        self.evaluate_beam_info()
        self._beam_info_dict["label"] = aperture.name
        self.re_emit_values()

    def slits_gap_changed(self, size: tuple[float, float]):
        """Method called when the slits gap changes.

        Args:
            Two floats - beam size in microns
        """
        self._beam_size_dict["slits"] = size
        self._beam_info_dict["label"] = "slits"
        self.evaluate_beam_info()
        self.re_emit_values()

    def set_beam_position_on_screen(self, beam_x_y: list[int, int]):
        """Sets beam mark position on screen.
        #TODO move method to sample_view

        Args:
            Position [x, y] in pixels.
        """
        self._beam_position_on_screen = beam_x_y
        self.emit("beamPosChanged", (self._beam_position_on_screen,))

    def get_slits_gap(self) -> tuple[float, float]:
        """Get the beam size from the slits gap.

        Returns:
            Two-item tuple with horizontal and vertical beam size in microns
        """
        self.evaluate_beam_info()
        return self._beam_size_dict["slits"]

    def set_slits_gap(self, width_microns: int, height_microns: int):
        """Sets slits gap in microns.

        Args:
            width and height in microns.
        """
        if self.slits:
            self.slits.set_horizontal_gap(width_microns / 1000.0)
            self.slits.set_vertical_gap(height_microns / 1000.0)

    def get_aperture_pos_name(self) -> str:
        """Get the name of the current aperture.

        Returns:
             name of current aperture position
        """
        return self.aperture.get_current_pos_name()

    def get_defined_beam_size(self) -> dict:
        """Get the predefined beam labels and size.

        Returns:
            Dictionary with lists of available beam size labels
            and the corresponding size (width,height) tuples.
            ``{"label": [str, str, ...], "size": [(w,h), (w,h), ...]}``
        """
        labels = []
        values = []
        if self._definer_type == "slits":
            return {
                "label": ["low", "high"],
                "size": [self.slits.get_min_limits(), self.slits.get_max_limits()],
            }

        if self._definer_type == "aperture":
            _enum = self.aperture.VALUES
        elif self._definer_type == "definer":
            _enum = self.definer.VALUES

        for value in _enum:
            _nam = value.name
            if _nam not in ["IN", "OUT", "UNKNOWN"]:
                labels.append(_nam)
                if self._definer_type == "aperture":
                    values.append((value.value[0] / 1000.0, value.value[0] / 1000.0))
                else:
                    values.append(value.value)
        return {"label": labels, "size": values}

    def get_available_size(self) -> dict:
        """Get the available predefined beam definer configuration.

        Returns:
            ``{"type": ["aperture"], "values": [labels]}`` or
            ``{"type": ["definer"], "values": [labels]}`` or
            ``{"type": ["width", "height"], "values":
                       [low_lim_w, high_lim_w, low_lim_h, high_lim_h]}``
        """
        if self._definer_type == "aperture":
            # get list of the available apertures
            return {
                "type": ["aperture"],
                "values": self.aperture.get_diameter_size_list(),
            }

        if self._definer_type == "definer":
            # get list of the available definer positions
            return {
                "type": ["definer"],
                "values": self.definer.get_predefined_positions_list(),
            }

        if self._definer_type == "slits":
            # get the list of the slits motors range
            _low_w, _low_h = self.slits.get_min_limits()
            _high_w, _high_h = self.slits.get_max_limits()
            return {
                "type": ["width", "height"],
                "values": [_low_w, _high_w, _low_h, _high_h],
            }

        return {}

    def set_value(self, size: list[float, float] | str | None = None):
        """Set the beam size.

        Args:
            size: List of width and  height in micrometers or
                  Aperture or definer definer name as string.
        Raises:
            RuntimeError: Beam definer not configured
                          Size out of the limits.
            TypeError: Wrong size type.
        """
        msg = "Incorrect input value for "
        if self._definer_type in (self.slits, "slits"):
            if not isinstance(size, list):
                msg += "slits"
                raise TypeError(msg)
            self.slits.set_horizontal_gap(size[0])
            self.slits.set_vertical_gap(size[1])

        if self._definer_type in (self.aperture, "aperture"):
            if not isinstance(size, str):
                msg += "aperture"
                raise TypeError(msg)
            self.aperture.set_value(self.aperture.VALUES[size], timeout=2)

        if self._definer_type in (self.definer, "definer"):
            if not isinstance(size, str):
                msg += "definer"
                raise TypeError(msg)
            self.definer.set_value(self.definer.VALUES[size], timeout=2)

    def _is_beam(self) -> bool:
        """Check if there is beam.

        Returns:
            ``True`` if beam present, ``False`` otherwise
        """
        if not self._check_beam:
            return True

        beam = self.get_value()
        return all(
            x1 <= x2
            for (x1, x2) in zip(self._check_beam, (beam[0], beam[1]), strict=False)
        )
