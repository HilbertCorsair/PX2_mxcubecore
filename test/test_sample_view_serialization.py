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
"""Serialization checks for the `SampleView` shapes.

`as_dict` is the only boundary between the shape model and its clients (the web
UI, XML-RPC, and from there ISPyB's grid_info). It used to be written as
`copy.deepcopy(vars(self))`, which pulled the `SampleView` hardware object into
the copy; `HardwareObject` overrides `__getstate__`/`__setstate__`, so every
serialization re-entered the hardware repository and blew up as soon as the
object's name did not resolve.

These tests pin down what replaced it: an explicit field contract, a serializer
that touches no hardware, and a deserializer that only writes declared fields.
"""

import pytest
from test.sample_view_geometry_stubs import (
    BEAM_POSITION,
    BEAM_SIZE,
    HIDE_GRID_THRESHOLD,
    PIXELS_PER_MM,
    make_sample_view,
)

from mxcubecore.HardwareObjects.SampleView import (
    Grid,
    Line,
    Point,
    Shape,
    TwoDPoint,
)
from mxcubecore.model import queue_model_objects as qmo

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


MOTOR_NAMES = ("omega", "phiy", "phiz", "sampx", "sampy")

MPOS = {"omega": 0.0, "phiy": 0.15, "phiz": 0.099, "sampx": -0.02, "sampy": 0.03}
MPOS_END = {"omega": 0.0, "phiy": 0.25, "phiz": 0.199, "sampx": -0.12, "sampy": 0.13}

# Grid drawn 100 px right of and 200 px below the top-left corner.
GRID_SCREEN_COORD = (100, 200)
GRID_WIDTH = 105
GRID_HEIGHT = 84


@pytest.fixture
def motor_names():
    """`CentredPosition` reads the motor names off a class attribute.

    `SampleView.init` normally sets it; restore it afterwards so this module
    cannot leak its names into the rest of the suite.
    """
    previous = qmo.CentredPosition.DIFFRACTOMETER_MOTOR_NAMES
    qmo.CentredPosition.DIFFRACTOMETER_MOTOR_NAMES = MOTOR_NAMES
    yield MOTOR_NAMES
    qmo.CentredPosition.DIFFRACTOMETER_MOTOR_NAMES = previous


@pytest.fixture
def sample_view(monkeypatch, motor_names):  # noqa: ARG001
    return make_sample_view(monkeypatch)


def make_grid(sample_view):
    """A grid registered with the sample view, as the web adapter would."""
    grid = sample_view.add_shape_from_mpos([MPOS], GRID_SCREEN_COORD, "G")
    grid.width = GRID_WIDTH
    grid.height = GRID_HEIGHT
    grid.num_cols = 7
    grid.num_rows = 6
    grid.cell_width = 15
    grid.cell_height = 14
    return grid


def test_shapes_have_no_hardware_back_reference(sample_view):
    """The Shape -> SampleView back-pointer is what broke the old deepcopy."""
    point = sample_view.add_shape_from_mpos([MPOS], (10, 20), "P")

    assert not hasattr(point, "shapes_hw_object")

    sample_view.delete_shape(point.id)
    assert sample_view.get_shape(point.id) is None


def test_point_serializes_declared_fields_only(sample_view):
    point = sample_view.add_shape_from_mpos([MPOS], (10, 20), "P")

    d = point.as_dict()

    assert set(d) == set(Shape.SERIAL_FIELDS) | {"motor_positions"}
    assert "cp_list" not in d
    assert "shapes_hw_object" not in d
    assert d["t"] == "P"
    assert d["screen_coord"] == (10, 20)
    # A point carries a single centred position, reported as a plain dict.
    assert d["motor_positions"] == MPOS


def test_two_d_point_serializes_like_a_point(sample_view):
    tdp = sample_view.add_shape_from_mpos([MPOS], (10, 20), "2DP")

    d = tdp.as_dict()

    assert d["t"] == "2DP"
    assert d["motor_positions"] == MPOS


def test_line_reports_a_list_of_motor_positions(sample_view):
    """Was `str([...])` -- a Python repr, not something a client can parse."""
    line = sample_view.add_shape_from_mpos([MPOS, MPOS_END], (10, 20, 30, 40), "L")

    d = line.as_dict()

    assert d["motor_positions"] == [MPOS, MPOS_END]


def test_grid_geometry_is_expressed_against_the_beam(sample_view):
    grid = make_grid(sample_view)

    d = grid.as_dict()

    # x1/y1 place the grid origin relative to the beam, in mm.
    assert d["x1"] == pytest.approx(
        -(BEAM_POSITION[0] - GRID_SCREEN_COORD[0]) / PIXELS_PER_MM[0]
    )
    assert d["y1"] == pytest.approx(
        -(BEAM_POSITION[1] - GRID_SCREEN_COORD[1]) / PIXELS_PER_MM[1]
    )
    assert d["dx_mm"] == pytest.approx(GRID_WIDTH / PIXELS_PER_MM[0])
    assert d["dy_mm"] == pytest.approx(GRID_HEIGHT / PIXELS_PER_MM[1])
    assert (d["steps_x"], d["steps_y"]) == (grid.num_cols, grid.num_rows)
    assert d["angle"] == 0


def test_grid_snapshots_the_calibration_when_added(sample_view):
    """add_shape seeds the snapshot, so a grid is serializable straight away."""
    grid = make_grid(sample_view)

    assert grid.pixels_per_mm == PIXELS_PER_MM
    assert grid.beam_pos == BEAM_POSITION
    assert (grid.beam_width, grid.beam_height) == BEAM_SIZE


def test_serialization_reads_no_hardware(sample_view):
    """`_emit_shapes_updated` serializes every shape on every shape change.

    Reading the calibration in `as_dict` therefore put Tango traffic behind
    every motor state change; the values are cached on the shape instead.
    """
    grid = make_grid(sample_view)

    reads = (sample_view.diffractometer.read_count, sample_view.beam.read_count)

    for _ in range(3):
        grid.as_dict()

    assert (sample_view.diffractometer.read_count, sample_view.beam.read_count) == reads

    grid.update_position(sample_view.motor_positions_to_screen)

    assert sample_view.diffractometer.read_count > reads[0]
    assert sample_view.beam.read_count > reads[1]


def test_grid_hidden_when_omega_is_far_from_the_shape(sample_view):
    """The hide threshold now comes from the sample view, not a back-pointer."""
    grid = make_grid(sample_view)

    sample_view.motors["omega"].set_value(HIDE_GRID_THRESHOLD * 4)
    grid.update_position(sample_view.motor_positions_to_screen)
    assert grid.state == "HIDDEN"

    sample_view.motors["omega"].set_value(0)
    grid.update_position(sample_view.motor_positions_to_screen)
    assert grid.state == "SAVED"


def test_update_from_dict_writes_only_declared_fields(sample_view):
    point = sample_view.add_shape_from_mpos([MPOS], (10, 20), "P")
    original_id = point.id

    point.update_from_dict(
        {
            "state": "TMP",
            "user_state": "HIDDEN",
            "screen_coord": (30, 40),
            "id": "P999",
            "t": "G",
            "shapes_hw_object": "injected",
            "motor_positions": {"omega": 42},
            "not_a_field": 1,
        }
    )

    assert point.state == "TMP"
    assert point.user_state == "HIDDEN"
    assert point.screen_coord == (30, 40)
    # Server-owned and unknown keys are ignored, not silently applied.
    assert point.id == original_id
    assert point.t == "P"
    assert not hasattr(point, "shapes_hw_object")
    assert not hasattr(point, "not_a_field")
    assert point.get_centred_position().omega == MPOS["omega"]


def test_update_from_dict_cannot_overwrite_the_grid_calibration(sample_view):
    grid = make_grid(sample_view)

    grid.update_from_dict(
        {
            "num_cols": 12,
            "pixels_per_mm": [1, 1],
            "beam_pos": [0, 0],
            "beam_width": 9,
            "beam_height": 9,
            "result": "tampered",
        }
    )

    assert grid.num_cols == 12
    assert grid.pixels_per_mm == PIXELS_PER_MM
    assert grid.beam_pos == BEAM_POSITION
    assert (grid.beam_width, grid.beam_height) == BEAM_SIZE
    assert grid.result is None


def test_grid_result_path_is_a_declared_field(sample_view):
    """It used to spring into existence only once a mesh had run."""
    grid = make_grid(sample_view)

    assert "result_data_path" in grid.as_dict()

    sample_view.set_grid_data(grid.id, {"1": [0, 0, 0, 0]}, "/data/mesh.h5")

    d = grid.as_dict()
    assert d["result_data_path"] == "/data/mesh.h5"
    assert d["result"] == {"1": [0, 0, 0, 0]}


@pytest.mark.parametrize("cls", [Point, TwoDPoint, Line, Grid])
def test_read_only_fields_are_never_writable(cls, motor_names):  # noqa: ARG001
    shape = cls([MPOS, MPOS_END], (0, 0))

    for field in cls.READ_ONLY_FIELDS:
        assert field not in shape.writable_fields()
        # "result" only exists on grids; the rest are on every shape.
        if field in cls.SERIAL_FIELDS:
            assert hasattr(shape, field)
