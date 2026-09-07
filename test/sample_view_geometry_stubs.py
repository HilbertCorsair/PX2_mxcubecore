# encoding: utf-8
#
# This file is part of MXCuBE.
#
# MXCuBE is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# MXCuBE is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with MXCuBE. If not, see <https://www.gnu.org/licenses/>.
"""Hardware stubs shared by the sample-view tests.

Not a test module: it holds the fake motors, fake beam, fake diffractometer and
the `SampleView` factory used by `test_sample_view_move_to_beam.py`,
`test_sample_centring_transpose.py` and `test_sample_view_serialization.py`.
"""

from types import SimpleNamespace

from mxcubecore import HardwareRepository as HWR
from mxcubecore.HardwareObjects.sample_centring import CentringMotor
from mxcubecore.HardwareObjects.SampleView import SampleView

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


# Optical centre of the 1360x1024 OAV frame, per webconfig/beam.yaml.
BEAM_POSITION = (680, 512)

# Beam size in mm. Anisotropic for the same reason as the calibration below.
BEAM_SIZE = (0.05, 0.03)

# Threshold, in degrees of omega, past which a grid is hidden.
HIDE_GRID_THRESHOLD = 5

# Deliberately anisotropic: equal values would hide an x/y mix-up in the
# calibration, which is exactly the class of bug these tests exist to catch.
PIXELS_PER_MM = (526.0, 480.0)

# Matches webconfig/sample_view.yaml. No motor runs against its screen axis:
# phiy's -1 was fitted while phiy was believed to carry the horizontal and
# was dropped once the transpose put it on the vertical.
MOTOR_DIRECTIONS = {"omega": 1, "phiy": 1, "phiz": 1, "sampx": 1, "sampy": 1}

# phiz sits at its centring_reference_position.
REFERENCE_POSITIONS = {"phiz": 0.099}

START_POSITIONS = {
    "omega": 0.0,
    "phiy": 0.15,
    "phiz": 0.099,
    "sampx": -0.02,
    "sampy": 0.03,
}


class FakeMotor:
    """Minimal stand-in for an `AbstractMotor`, always settled."""

    def __init__(self, name, value):
        self.actuator_name = name
        self._value = float(value)

    def get_value(self):
        return self._value

    def set_value(self, value, timeout=None):
        self._value = float(value)

    def set_value_relative(self, delta, timeout=None):
        self._value += float(delta)

    def wait_ready(self, timeout=None):
        """No-op: the fake motor never moves for real."""

    def is_ready(self):
        return True

    def connect(self, *args, **kwargs):
        """No-op: nothing subscribes to a fake motor."""


class FakeBeam:
    """Minimal stand-in for the beam, counting how often it is read."""

    def __init__(self, position=BEAM_POSITION, size=BEAM_SIZE):
        self._position = position
        self._size = size
        self.read_count = 0

    def get_beam_position_on_screen(self):
        self.read_count += 1
        return self._position

    def get_value(self):
        self.read_count += 1
        return (self._size[0], self._size[1], "ellipse", "50x30")


class FakeDiffractometer:
    """Records the position dicts handed to `set_value_motors`."""

    def __init__(self, motors, pixels_per_mm=PIXELS_PER_MM):
        self.motors_hwobj_dict = motors
        self._pixels_per_mm = pixels_per_mm
        self.moves = []
        # Serialization must not read the hardware; the tests assert on this.
        self.read_count = 0

    @property
    def omega(self):
        return self.motors_hwobj_dict["omega"]

    def get_pixels_per_mm(self):
        self.read_count += 1
        return self._pixels_per_mm

    def wait_status_ready(self, timeout=None):
        """No-op: the fake diffractometer is always ready."""

    def set_value_motors(self, positions, simultaneous=True, timeout=None):
        self.moves.append(dict(positions))
        for role, value in positions.items():
            self.motors_hwobj_dict[role].set_value(value)

    def save_centring_positions(self):
        """No-op: nothing to persist in the fake."""


def make_centring_motors(motors):
    """Wrap fake motors in real `CentringMotor`s, as `SampleView.init` does."""
    return {
        role: CentringMotor(
            motor,
            reference_position=REFERENCE_POSITIONS.get(role),
            direction=MOTOR_DIRECTIONS[role],
        )
        for role, motor in motors.items()
    }


def make_sample_view(
    monkeypatch,
    transposed=False,
    omega_phase_offset=0.0,
    start_positions=None,
    pixels_per_mm=PIXELS_PER_MM,
):
    """A bare `SampleView` wired to stubbed motors, beam and diffractometer.

    Built with `__new__` on purpose: `init()` would pull in the whole
    HardwareObject/xml-config machinery, which has nothing to do with the
    geometry under test.

    Args:
        monkeypatch: pytest fixture, used to swap out `HWR.beamline`.
        transposed: value of `transposed_camera_axes`.
        omega_phase_offset: value of `omega_phase_offset`, in degrees.
        start_positions: {role: position}, defaults to `START_POSITIONS`.
        pixels_per_mm: (x, y) calibration reported by the diffractometer.
    """
    start = dict(start_positions or START_POSITIONS)
    motors = {role: FakeMotor(role, value) for role, value in start.items()}

    view = SampleView.__new__(SampleView)
    view._shapes = {}
    view.hide_grid_threshold = HIDE_GRID_THRESHOLD
    view.chi_angle = 0
    view.transposed_camera_axes = transposed
    view.omega_phase_offset = omega_phase_offset
    view.centring_motors = make_centring_motors(motors)

    diffractometer = FakeDiffractometer(motors, pixels_per_mm)
    beam = FakeBeam()
    monkeypatch.setattr(
        HWR,
        "beamline",
        SimpleNamespace(
            beam=beam,
            diffractometer=diffractometer,
            sample_view=view,
        ),
        raising=False,
    )

    view.diffractometer = diffractometer
    view.beam = beam
    view.motors = motors
    return view
