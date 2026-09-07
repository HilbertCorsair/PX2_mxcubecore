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
"""Geometry checks for `SampleView.move_to_beam` ("Go to Beam").

These run without any hardware: the beam, the diffractometer and the centring
motors are stubbed, so the only thing under test is the coordinate maths.

Every case runs in both orientations. On the **upstream** layout the spindle
lies along the screen's horizontal axis, so `phiy` (the spindle-parallel
translation) takes the horizontal offset and the centring table takes the
vertical one. With `transposed_camera_axes` the spindle stands vertically in
the camera frame -- the PX2 case -- and the two swap over.

The suite pins down that both components of the click offset are acted upon,
that they reach the right motors in each orientation, and that `move_to_beam`
and `motor_positions_to_screen` remain each other's inverse.
"""

import pytest

from test.sample_view_geometry_stubs import (
    BEAM_POSITION,
    MOTOR_DIRECTIONS,
    PIXELS_PER_MM,
    START_POSITIONS,
    make_sample_view,
)

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


@pytest.fixture(params=[False, True], ids=["upstream", "transposed"])
def sample_view(request, monkeypatch):
    """A stubbed `SampleView`, once per orientation."""
    view = make_sample_view(monkeypatch, transposed=request.param)
    view.transposed = request.param
    return view


def _click_offsets(click_x, click_y):
    """Click offset from the beam mark, in millimetres of screen."""
    return (
        (click_x - BEAM_POSITION[0]) / PIXELS_PER_MM[0],
        (click_y - BEAM_POSITION[1]) / PIXELS_PER_MM[1],
    )


def _split(view, dx, dy):
    """Expected (along-spindle, transverse) split of a screen offset."""
    return (dy, dx) if view.transposed else (dx, dy)


def test_move_to_beam_moves_both_axes_at_omega_zero(sample_view):
    """At omega = 0 neither component may vanish, in either orientation."""
    click_x, click_y = 780, 612
    dx, dy = _click_offsets(click_x, click_y)
    d_along, d_trans = _split(sample_view, dx, dy)

    sample_view.move_to_beam(click_x, click_y)

    move = sample_view.diffractometer.moves[-1]

    # phiy carries the along-spindle component, in its own direction sense.
    assert move["phiy"] == pytest.approx(
        START_POSITIONS["phiy"] + MOTOR_DIRECTIONS["phiy"] * d_along
    )

    # At omega = 0 the whole transverse component goes to sampy.
    assert move["sampy"] == pytest.approx(START_POSITIONS["sampy"] + d_trans)
    assert move["sampx"] == pytest.approx(START_POSITIONS["sampx"])

    # Both components are genuinely non-zero -- the failure this suite exists
    # to catch is one of them silently going nowhere.
    assert move["phiy"] != pytest.approx(START_POSITIONS["phiy"])
    assert move["sampy"] != pytest.approx(START_POSITIONS["sampy"])

    # phiz stays parked: moving it would take the spindle off the beam.
    assert "phiz" not in move


def test_move_to_beam_transverse_swaps_to_sampx_at_omega_90(sample_view):
    """At omega = 90 deg the centring table carries the transverse on sampx."""
    sample_view.motors["omega"].set_value(90.0)

    click_x, click_y = 780, 612
    dx, dy = _click_offsets(click_x, click_y)
    d_along, d_trans = _split(sample_view, dx, dy)

    sample_view.move_to_beam(click_x, click_y)

    move = sample_view.diffractometer.moves[-1]

    assert move["phiy"] == pytest.approx(
        START_POSITIONS["phiy"] + MOTOR_DIRECTIONS["phiy"] * d_along
    )
    # [0, d_trans] rotated by -omega gives (-d_trans, 0); sampx is current - that.
    assert move["sampx"] == pytest.approx(START_POSITIONS["sampx"] + d_trans)
    assert move["sampy"] == pytest.approx(START_POSITIONS["sampy"], abs=1e-9)


def test_transposed_sends_vertical_to_phiy_not_the_table(monkeypatch):
    """The orientations must disagree, or the flag is doing nothing.

    A purely vertical click at omega = 0 is the discriminating case: upstream
    it moves only the table, transposed it moves only phiy.
    """
    click_x, click_y = BEAM_POSITION[0], 612

    upstream = make_sample_view(monkeypatch, transposed=False)
    upstream.move_to_beam(click_x, click_y)
    up_move = upstream.diffractometer.moves[-1]

    transposed = make_sample_view(monkeypatch, transposed=True)
    transposed.move_to_beam(click_x, click_y)
    tr_move = transposed.diffractometer.moves[-1]

    # Upstream: all of it on the table, phiy untouched.
    assert up_move["phiy"] == pytest.approx(START_POSITIONS["phiy"])
    assert up_move["sampy"] != pytest.approx(START_POSITIONS["sampy"])

    # Transposed: all of it on phiy, table untouched.
    assert tr_move["phiy"] != pytest.approx(START_POSITIONS["phiy"])
    assert tr_move["sampy"] == pytest.approx(START_POSITIONS["sampy"])
    assert tr_move["sampx"] == pytest.approx(START_POSITIONS["sampx"])


@pytest.mark.parametrize("omega", [0.0, 45.0, 90.0, 180.0])
def test_move_to_beam_round_trips_through_motor_positions_to_screen(
    sample_view, omega
):
    """The clicked pixel must map back to itself after the move."""
    sample_view.motors["omega"].set_value(omega)

    click_x, click_y = 760, 450
    sample_view.move_to_beam(click_x, click_y)

    target = dict(sample_view.diffractometer.moves[-1])

    # Re-project against the positions the motors had *before* the move.
    for role, value in START_POSITIONS.items():
        sample_view.motors[role].set_value(value)
    sample_view.motors["omega"].set_value(omega)

    screen_x, screen_y = sample_view.motor_positions_to_screen(target)

    assert screen_x == pytest.approx(click_x, abs=1)
    assert screen_y == pytest.approx(click_y, abs=1)


def test_motor_positions_to_screen_tolerates_missing_phiz(sample_view):
    """A chi-less move_to_beam target carries no phiz; that must not raise."""
    positions = {
        "phiy": START_POSITIONS["phiy"],
        "sampx": START_POSITIONS["sampx"],
        "sampy": START_POSITIONS["sampy"],
    }

    screen_x, screen_y = sample_view.motor_positions_to_screen(positions)

    # No displacement from the current positions -> the beam mark itself.
    assert screen_x == pytest.approx(BEAM_POSITION[0], abs=1)
    assert screen_y == pytest.approx(BEAM_POSITION[1], abs=1)


def test_move_to_beam_without_calibration_does_not_move(sample_view):
    """A missing pixels_per_mm must abort, not raise mid-calculation."""
    sample_view.diffractometer._pixels_per_mm = (None, None)

    sample_view.move_to_beam(760, 450)

    assert sample_view.diffractometer.moves == []


def test_omega_phase_offset_rotates_the_table_decomposition(monkeypatch):
    """A 90 deg offset must make omega = 0 behave like omega = 90."""
    click_x, click_y = 780, 612

    shifted = make_sample_view(monkeypatch, omega_phase_offset=90.0)
    shifted.move_to_beam(click_x, click_y)
    shifted_move = shifted.diffractometer.moves[-1]

    plain = make_sample_view(monkeypatch)
    plain.motors["omega"].set_value(90.0)
    plain.move_to_beam(click_x, click_y)
    plain_move = plain.diffractometer.moves[-1]

    assert shifted_move["sampx"] == pytest.approx(plain_move["sampx"])
    assert shifted_move["sampy"] == pytest.approx(plain_move["sampy"])


def test_move_to_beam_drives_the_click_towards_the_mark(sample_view):
    """The move must take the clicked feature *to* the mark, not away from it.

    The suite otherwise only pins down which motor receives each component,
    never which way it turns -- and a sign fitted for the wrong projection is
    exactly how the vertical came to be inverted on the beamline. Stated in
    screen terms so it stays true whatever `motor_directions` says: after the
    move, the feature that used to sit on the mark must have travelled by
    minus the click offset.
    """
    click_x, click_y = 780, 612
    dx, dy = _click_offsets(click_x, click_y)

    sample_view.move_to_beam(click_x, click_y)
    target = sample_view.diffractometer.moves[-1]

    # The motors are now at `target`; re-project the pre-move positions.
    old_centre_x, old_centre_y = sample_view.motor_positions_to_screen(
        START_POSITIONS
    )

    assert old_centre_x == pytest.approx(
        BEAM_POSITION[0] - dx * PIXELS_PER_MM[0], abs=1
    )
    assert old_centre_y == pytest.approx(
        BEAM_POSITION[1] - dy * PIXELS_PER_MM[1], abs=1
    )

    # And the along-spindle motor turned in its own positive sense for a
    # click below (transposed) or right of (upstream) the mark.
    d_along, _ = _split(sample_view, dx, dy)
    assert MOTOR_DIRECTIONS["phiy"] * (
        target["phiy"] - START_POSITIONS["phiy"]
    ) == pytest.approx(d_along)


def test_centred_point_from_coord_agrees_with_move_to_beam(sample_view):
    """A point built from a click must be where move_to_beam would drive to.

    Both go through the same decomposition, so they can only disagree through
    the direction bookkeeping on the way out -- which is what regressed when
    `get_centred_point_from_coord` hardcoded a negation valid for one sign of
    `phiy` only.
    """
    click_x, click_y = 780, 612

    point = sample_view.get_centred_point_from_coord(click_x, click_y)

    sample_view.move_to_beam(click_x, click_y)
    target = sample_view.diffractometer.moves[-1]

    for role in target:
        assert point[role] == pytest.approx(target[role]), role

    # phiz is not moved, so the point carries its parked position.
    assert point["phiz"] == pytest.approx(START_POSITIONS["phiz"])
