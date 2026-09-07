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
"""Orientation checks for `sample_centring.center` (3-click centring).

The clicks are synthesised from a known sample offset, so the test asserts that
centring *recovers that offset* -- in both orientations. `center()` works in the
goniometer frame (X along the spindle, Y transverse to it); with
`transposed=True` the spindle stands vertically in the camera frame and the
clicks, the calibration and the beam centre are all transposed on the way in.

No hardware: the motors are the stubs from `sample_view_geometry_stubs`.
"""

import math

import pytest

from mxcubecore.HardwareObjects import sample_centring
from test.sample_view_geometry_stubs import (
    BEAM_POSITION,
    MOTOR_DIRECTIONS,
    PIXELS_PER_MM,
    REFERENCE_POSITIONS,
    START_POSITIONS,
    FakeMotor,
    make_centring_motors,
)

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


# The sample offset the synthetic clicks encode, in mm of centring table.
TABLE_DX = 0.030  # what sampx must pick up
TABLE_DY = -0.020  # what sampy must pick up

# Where the feature sits along the spindle, and the constant term of its
# transverse excursion -- both absolute screen positions, in mm.
ALONG_MM = 1.4
TRANSVERSE_OFFSET_MM = 1.0

# center() drives omega over 180 deg in n_points steps.
OMEGAS = (0.0, 90.0, 180.0)


@pytest.fixture
def motors():
    return {
        role: FakeMotor(role, value) for role, value in START_POSITIONS.items()
    }


def _synthetic_clicks(transposed):
    """Screen pixels the operator would click, for a known table offset.

    The transverse screen position of a feature held off the rotation axis
    traces `offset + dy*cos(omega) + dx*sin(omega)`; the along-spindle position
    does not depend on omega at all. That is exactly the model `center()`
    inverts, so feeding it back must return `dx`/`dy`.
    """
    clicks = []
    for omega in OMEGAS:
        rad = math.radians(omega)
        transverse_mm = (
            TRANSVERSE_OFFSET_MM
            + TABLE_DY * math.cos(rad)
            + TABLE_DX * math.sin(rad)
        )
        if transposed:
            x_mm, y_mm = transverse_mm, ALONG_MM
        else:
            x_mm, y_mm = ALONG_MM, transverse_mm
        clicks.append((x_mm * PIXELS_PER_MM[0], y_mm * PIXELS_PER_MM[1]))
    return clicks


def _run_centring(centring_motors, transposed):
    procedure = sample_centring.start(
        centring_motors,
        PIXELS_PER_MM[0],
        PIXELS_PER_MM[1],
        BEAM_POSITION[0],
        BEAM_POSITION[1],
        chi_angle=0,
        n_points=len(OMEGAS),
        transposed=transposed,
    )

    for click_x, click_y in _synthetic_clicks(transposed):
        sample_centring.user_click(click_x, click_y, wait=True)

    return procedure.get(timeout=10)


def _expected_phiy(transposed):
    """phiy target: the along-spindle offset from the beam mark."""
    if transposed:
        beam_along_mm = BEAM_POSITION[1] / PIXELS_PER_MM[1]
    else:
        beam_along_mm = BEAM_POSITION[0] / PIXELS_PER_MM[0]

    return START_POSITIONS["phiy"] + MOTOR_DIRECTIONS["phiy"] * (
        ALONG_MM - beam_along_mm
    )


@pytest.mark.parametrize(
    "transposed", [False, True], ids=["upstream", "transposed"]
)
def test_centring_recovers_the_table_offset(motors, transposed):
    """Centring must return the offset the synthetic clicks were built from."""
    centring_motors = make_centring_motors(motors)

    centred_pos = _run_centring(centring_motors, transposed)

    assert centred_pos[motors["sampx"]] == pytest.approx(
        START_POSITIONS["sampx"] + MOTOR_DIRECTIONS["sampx"] * TABLE_DX,
        abs=1e-6,
    )
    assert centred_pos[motors["sampy"]] == pytest.approx(
        START_POSITIONS["sampy"] + MOTOR_DIRECTIONS["sampy"] * TABLE_DY,
        abs=1e-6,
    )
    assert centred_pos[motors["phiy"]] == pytest.approx(
        _expected_phiy(transposed), abs=1e-6
    )

    # phiz has a centring_reference_position, so it is parked rather than used
    # for the transverse correction -- matching what move_to_beam does.
    assert centred_pos[motors["phiz"]] == pytest.approx(
        REFERENCE_POSITIONS["phiz"]
    )


def test_transposed_centring_differs_from_upstream(motors):
    """The flag must change the outcome, or it is not wired through."""
    upstream = _run_centring(make_centring_motors(motors), transposed=False)
    upstream_sampx = upstream[motors["sampx"]]
    upstream_phiy = upstream[motors["phiy"]]

    for role, value in START_POSITIONS.items():
        motors[role].set_value(value)

    transposed = _run_centring(make_centring_motors(motors), transposed=True)

    # Same clicked *pixels* would give a different answer; here the clicks are
    # themselves transposed, so what must match is the recovered offset while
    # the along-spindle reference differs.
    assert transposed[motors["sampx"]] == pytest.approx(
        upstream_sampx, abs=1e-6
    )
    assert transposed[motors["phiy"]] != pytest.approx(upstream_phiy)
