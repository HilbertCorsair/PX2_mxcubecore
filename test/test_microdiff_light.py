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
"""Tests for the MicrodiffLight on/off switch.

The front light of the PX2 MD2 keeps reporting ``FrontLightIsOn == True``
after being switched off; only ``FrontLightLevel`` follows. These tests pin
down that the switch state is derived from both attributes and that every
write emits ``lightSwitchChanged``.
"""

import pytest

from mxcubecore.HardwareObjects import MicrodiffLight as microdiff_light
from mxcubecore.HardwareObjects.MicrodiffLight import MicrodiffLight

__copyright__ = """ Copyright © by the MXCuBE collaboration """
__license__ = "LGPLv3+"


class FakeChannel:
    """Minimal stand-in for a Tango/exporter ChannelObject."""

    def __init__(self, value):
        self.value = value
        self.writes = []

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.writes.append(value)
        self.value = value

    def connect_signal(self, signal_name, callable_func):
        pass


class StickyFlagChannel(FakeChannel):
    """A flag channel that ignores writes, like MD2's ``FrontLightIsOn``."""

    def set_value(self, value):
        self.writes.append(value)


def make_light(level=50.0, is_on=True, flag_cls=FakeChannel):
    """Build an initialised MicrodiffLight backed by fake channels."""
    light = MicrodiffLight("frontlight")

    channels = {
        "chanLightValue": FakeChannel(level),
        "chanLightIsOn": flag_cls(is_on),
    }

    light.get_channel_object = channels.get
    light.get_property = lambda name, default=None: (
        "0,100" if name == "limits" else default
    )
    light.init()

    return light, channels


@pytest.fixture(autouse=True)
def _no_readback_delay(monkeypatch):
    """Keep the bounded read-back wait from actually sleeping."""
    monkeypatch.setattr(microdiff_light, "SWITCH_READBACK_INTERVAL", 0.0)
    monkeypatch.setattr(microdiff_light, "SWITCH_READBACK_TIMEOUT", 0.0)


def collect_switch_signals(light):
    """Record every ``lightSwitchChanged`` payload the light emits."""
    emitted = []
    original_emit = light.emit

    def _emit(signal, *args):
        if signal == "lightSwitchChanged":
            emitted.append(args[0])
        return original_emit(signal, *args)

    light.emit = _emit
    return emitted


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("True", True),
        # bool("false") is True -- the exporter channels return raw strings.
        ("false", False),
        ("False", False),
    ],
)
def test_is_on_coerces_channel_value(raw, expected):
    light, _ = make_light(level=50.0, is_on=raw)
    assert light.is_on() is expected


def test_light_at_level_zero_reads_out_despite_the_flag():
    """The front light case: flag stuck on, level down."""
    light, _ = make_light(level=0.0, is_on=True)

    assert light.is_on() is False
    assert light.switch_value() == "OUT"


def test_switch_value_is_in_when_lit():
    light, _ = make_light(level=50.0, is_on=True)
    assert light.switch_value() == "IN"


def test_set_off_writes_the_flag_and_emits():
    light, channels = make_light(level=50.0, is_on=True)
    emitted = collect_switch_signals(light)

    light.set_off()

    assert channels["chanLightIsOn"].writes == [False]
    assert emitted[-1] == "OUT"


def test_set_off_takes_the_level_down_when_the_flag_is_ignored():
    light, channels = make_light(level=50.0, is_on=True, flag_cls=StickyFlagChannel)
    emitted = collect_switch_signals(light)

    light.set_off()

    assert channels["chanLightValue"].value == 0.0
    assert light.switch_value() == "OUT"
    assert emitted[-1] == "OUT"


def test_set_on_restores_the_previous_level():
    light, channels = make_light(level=50.0, is_on=True, flag_cls=StickyFlagChannel)
    light.set_off()

    emitted = collect_switch_signals(light)
    light.set_on()

    assert channels["chanLightValue"].value == 50.0
    assert light.switch_value() == "IN"
    assert emitted[-1] == "IN"


def test_set_on_falls_back_to_mid_range_without_a_remembered_level():
    light, channels = make_light(level=0.0, is_on=False, flag_cls=StickyFlagChannel)

    light.set_on()

    assert channels["chanLightValue"].value == 50.0


def test_set_switch_dispatches_on_the_in_out_string():
    light, channels = make_light(level=50.0, is_on=True)

    light.set_switch("OUT")
    assert channels["chanLightIsOn"].writes == [False]

    light.set_switch("IN")
    assert channels["chanLightIsOn"].writes == [False, True]


def test_level_change_emits_the_switch_state():
    """The button has to follow the intensity slider."""
    light, channels = make_light(level=0.0, is_on=True)
    emitted = collect_switch_signals(light)

    channels["chanLightValue"].value = 40.0
    light._on_value_update(40.0)

    assert emitted[-1] == "IN"

    channels["chanLightValue"].value = 0.0
    light._on_value_update(0.0)

    assert emitted[-1] == "OUT"


def test_limits_accept_both_the_yaml_and_the_xml_form():
    light = MicrodiffLight("frontlight")
    light.get_channel_object = lambda name: None
    light.get_property = lambda name, default=None: (
        "[0, 100]" if name == "limits" else default
    )
    light.init()

    assert light.get_limits() == (0.0, 100.0)


def test_unchanged_switch_state_is_not_re_emitted():
    """The level channel polls once a second; only real changes go out."""
    light, channels = make_light(level=50.0, is_on=True)
    emitted = collect_switch_signals(light)

    light._on_value_update(50.0)
    light._on_value_update(49.0)
    assert emitted == ["IN"]

    channels["chanLightValue"].value = 0.0
    light._on_value_update(0.0)
    assert emitted == ["IN", "OUT"]


def test_force_emit_signals_always_pushes_the_switch_state():
    light, _ = make_light(level=50.0, is_on=True)
    light._emit_switch()
    emitted = collect_switch_signals(light)

    light.force_emit_signals()

    assert emitted == ["IN"]
