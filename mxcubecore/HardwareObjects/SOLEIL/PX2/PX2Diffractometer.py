#
#  Project name: MXCuBE
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

"""SOLEIL Proxima 2A diffractometer based on AbstractDiffractometer.

Centring and sample-view geometry now live in ``SampleView``; this class
is a hardware-only facade over the MD2 application: it owns one shared
``Exporter`` connection and exposes the MD2 axes as in-process
``MD2MotorProxy`` instances, removing the need for a per-axis
``SOLEILMicrodiffMotor`` configuration file.
"""

import datetime
import logging
import os
import time
from ast import literal_eval
from enum import Enum
from math import isnan, sqrt

import beam_align
import gevent
import numpy as np
import scan_and_align
from anneal import anneal as anneal_procedure
from camera import camera
from detector import detector
from goniometer import goniometer

from mxcubecore import HardwareRepository as HWR
from mxcubecore.BaseHardwareObjects import HardwareObject, HardwareObjectState
from mxcubecore.HardwareObjects.abstract.AbstractDiffractometer import (
    AbstractDiffractometer,
    DiffractometerHead,
    DiffractometerPhase,
)
from mxcubecore.HardwareObjects.ExporterMotor import ExporterMotor
from mxcubecore.TaskUtils import task

__credits__ = ["SOLEIL"]
__version__ = "3.0"
__category__ = "General"


# Raw MD2 Exporter ``State`` string -> framework HardwareObjectState. Shared by
# the diffractometer and its in-process motor proxies so both translate the same
# way. Unrecognised values fall back to UNKNOWN.
EXPORTER_TO_HWSTATE = {
    "Ready": HardwareObjectState.READY,
    "Standby": HardwareObjectState.READY,
    "STANDBY": HardwareObjectState.READY,
    "LowLim": HardwareObjectState.READY,
    "HighLim": HardwareObjectState.READY,
    "Moving": HardwareObjectState.BUSY,
    "MOVING": HardwareObjectState.BUSY,
    "Initializing": HardwareObjectState.BUSY,
    "Created": HardwareObjectState.UNKNOWN,
    "Unknown": HardwareObjectState.UNKNOWN,
    "Invalid": HardwareObjectState.FAULT,
    "Fault": HardwareObjectState.FAULT,
    "Offline": HardwareObjectState.OFF,
}


class _CallableBool(int):
    # Truthy/falsy like a bool AND callable like a method — lets the same
    # attribute satisfy callers that use property syntax (`if x.in_plate_mode`)
    # and legacy method syntax (`x.in_plate_mode()`).
    def __call__(self):
        return bool(self)

    def __repr__(self):
        return repr(bool(self))


class MD2MotorProxy(ExporterMotor):
    """In-process MD2 axis backed by the diffractometer's shared Exporter.

    Built and ``init()``-d directly by ``PX2Diffractometer`` rather than
    loaded by the YAML loader, so the per-motor wrapper YAML files are no
    longer needed. All proxies share the diffractometer's single Exporter
    connection.

    It subclasses the canonical ``ExporterMotor`` purely to inherit its
    device-queried, hardened limit logic (``get_limits`` /
    ``get_dynamic_limits`` / ``get_max_speed`` -- which map ``±inf`` to
    ``±sys.float_info.max`` so the values stay valid JSON). Only the wiring
    that differs -- a shared connection driven by ``register`` callbacks
    instead of per-motor channel objects -- is overridden below.
    """

    # Shared module-level translation table (see EXPORTER_TO_HWSTATE above);
    # kept as a class alias so existing references keep working.
    EXPORTER_TO_HWSTATE = EXPORTER_TO_HWSTATE

    def __init__(self, name, actuator_name, exporter):
        super().__init__(name)
        self.actuator_name = actuator_name
        self._exporter = exporter
        self._position_event = f"{actuator_name}Position"
        self._state_event = f"{actuator_name}State"

    def init(self):
        self._exporter.register(self._position_event, self.update_value)
        self._exporter.register(self._state_event, self._on_state_event)
        try:
            self._nominal_value = self.get_value()
        except Exception:
            self.log.exception(
                "Initial position read failed for %s", self.actuator_name
            )
        self.update_state(self.get_state())

    def get_value(self):
        # Mirrors ExporterMotor.get_value: a missing/absent MD2 axis reads back
        # as NaN (or None). Never hand a NaN up the stack -- it serialises to a
        # bare `NaN` token, which is invalid JSON and breaks parsing of the
        # whole beamline payload in the browser. Fall back to the last good
        # value; when the axis has never read (absent), that is None -> JSON
        # null, which the init loop uses to skip exposing the axis.
        value = self._exporter.read_property(self._position_event)
        if value is None or (isinstance(value, float) and isnan(value)):
            return self._nominal_value
        self._nominal_value = value
        return value

    def _set_value(self, value):
        self.update_state(HardwareObjectState.BUSY)
        self._exporter.write_property(self._position_event, value)

    def get_state(self):
        try:
            raw = self._exporter.read_property(self._state_event)
        except Exception:
            return HardwareObjectState.UNKNOWN
        return self.EXPORTER_TO_HWSTATE.get(raw, HardwareObjectState.UNKNOWN)

    def _on_state_event(self, raw):
        self.update_state(
            self.EXPORTER_TO_HWSTATE.get(raw, HardwareObjectState.UNKNOWN)
        )

    def abort(self):
        self._exporter.execute("abort")

    def stop(self):
        self.abort()

    # get_limits / get_dynamic_limits / get_max_speed are inherited from
    # ExporterMotor: they query getMotorLimits / getMotorDynamicLimits /
    # getMotorMaxSpeed on the shared Exporter and map ±inf to ±float_max so
    # the limits serialise to valid JSON (a bare `Infinity` token was what
    # aborted the browser's JSON.parse of the login payload).

    def home(self, timeout=None):
        self._exporter.execute("startHomingMotor", (self.actuator_name,))
        self.wait_ready(timeout)


class PX2Diffractometer(AbstractDiffractometer):
    """SOLEIL Proxima 2A MD2 diffractometer."""

    # Convention naming: omega for the rotation axis, phiz/phiy for alignment.
    #
    # This table states a hardware fact and nothing else: `phiy` IS AlignmentY.
    # It is not the place to encode how the goniometer is oriented in the OAV
    # frame -- that lives in sample_view.yaml as `transposed_camera_axes`,
    # because `centring_reference_position`, `rotation_reference`
    # (script: Change_AlignmentZ) and the ui.yaml motor labels all key off the
    # role names and would silently follow the wrong physical motor if this
    # mapping lied.
    MOTOR_ROLE_TO_MD2 = {
        "omega": "Omega",
        "phiy": "AlignmentY",
        "phiz": "AlignmentZ",
        "focus": "AlignmentX",
        "sampx": "CentringX",
        "sampy": "CentringY",
        "kappa": "Kappa",
        "kappa_phi": "Phi",
    }

    PHASE_FROM_MD2 = {
        "Centring": DiffractometerPhase.CENTRE,
        "DataCollection": DiffractometerPhase.COLLECT,
        "BeamLocation": DiffractometerPhase.SEE_BEAM,
        "Transfer": DiffractometerPhase.TRANSFER,
        "Unknown": DiffractometerPhase.UNKNOWN,
    }
    PHASE_TO_MD2 = {phase: name for name, phase in PHASE_FROM_MD2.items()}

    @staticmethod
    def _extract_shared_exporter(channel):
        """Return the Exporter instance backing an already-loaded channel.

        The exporter address is declared once, as the key of the YAML
        ``exporter:`` section; the loader starts a single cached Exporter
        for it before ``init()`` runs. We pull it off any channel so the
        address does not have to be restated as a configuration property.
        """
        if channel is None:
            raise RuntimeError(
                "PX2Diffractometer: no Exporter channel configured — "
                "check the 'exporter:' section in diffractometer.yaml"
            )
        exporter = getattr(channel, "_ExporterChannel__exporter", None)
        if exporter is None:
            raise RuntimeError(
                "PX2Diffractometer: channel %r is not an ExporterChannel" % channel
            )
        return exporter

    def __init__(self, name):
        super().__init__(name)
        self._exporter = None

        # Pre-declare motor and nstate role attributes so introspection
        # from the UI / Beamline layer doesn't warn when init() bails
        # out before the proxy loop (e.g. off-site, no Exporter).
        for _role in self.MOTOR_ROLE_TO_MD2:
            setattr(self, _role, None)
        for _role in ("beamstop", "capillary", "backlight", "frontlight"):
            setattr(self, _role, None)

        self.zoom = None
        self.omega_reference_motor = None
        self.omega_reference_par = None
        self.omega_reference_pos = [0, 0]
        self.reference_pos = [680, 512]
        self.zoom_centre = {"x": 680, "y": 512}
        self.pixels_per_mm_x = 0.0
        self.pixels_per_mm_y = 0.0
        self.beam_position = (680, 512)
        self.fast_shutter_is_open = False
        self.collecting = False

        self.chan_calib_x = None
        self.chan_calib_y = None
        self.chan_current_phase = None
        self.chan_head_type = None
        self.chan_fast_shutter_is_open = None
        self.chan_state = None
        self.chan_status = None
        self.chan_scintillator_position = None
        self.chan_capillary_position = None
        self.cmd_start_set_phase = None
        self.cmd_start_auto_focus = None
        self.cmd_get_omega_scan_limits = None
        self.cmd_save_centring_positions = None

        self.current_state = None
        self.current_status = None

        # SOLEIL PyTango helpers used by bpc/aa/anneal/scintillator etc.
        self.goniometer = goniometer()
        self.camera = camera()
        self.detector = detector()

        self.md2_to_mxcube = {md2: role for role, md2 in self.MOTOR_ROLE_TO_MD2.items()}
        self.mxcube_to_md2 = dict(self.MOTOR_ROLE_TO_MD2)

    def init(self):
        # Skip AbstractDiffractometer.init() — it would try to look up motor
        # roles via get_object_by_role(), but our motors are in-process
        # proxies built below and not loaded by the YAML loader.
        HardwareObject.init(self)
        self.username = self.get_property("username") or self.username

        # Standard channels were already attached by setup_commands_channels()
        # before init() runs. Grab the shared Exporter off one of them rather
        # than restating the address as a separate configuration property.
        self.chan_state = self.get_channel_object("State")
        self.chan_status = self.get_channel_object("Status")
        self._exporter = self._extract_shared_exporter(self.chan_state)

        configured_motors = list(
            self.config.motors or self.MOTOR_ROLE_TO_MD2.keys()
        )
        for role in configured_motors:
            md2_name = self.MOTOR_ROLE_TO_MD2.get(role)
            if not md2_name:
                self.log.warning(
                    "PX2Diffractometer: no MD2 mapping for motor role '%s'", role
                )
                continue
            proxy = MD2MotorProxy(role, md2_name, self._exporter)
            proxy.init()

            # Skip axes that aren't present on this MD2 head (e.g. kappa /
            # kappa_phi on a non-minikappa goniometer). Their Exporter position
            # reads back as NaN, which get_value() maps to None (no reading);
            # exposing such a proxy would leave a broken, unreadable motor in
            # the UI, so drop it.
            try:
                initial = proxy.get_value()
            except Exception:
                initial = None
            if initial is None:
                self.log.warning(
                    "PX2Diffractometer: motor '%s' (%s) is not readable on this "
                    "head; not exposing it to the UI",
                    role,
                    md2_name,
                )
                setattr(self, role, None)
                continue

            # Expose the in-process proxy so mxcubeweb's AdapterManager can reach
            # it (the UI reads motors from 'diffractometer.<role>' attributes):
            #  - _hwobj_container + _name make proxy.id == "diffractometer.<role>"
            #  - registering it in the repository under a leading-slash key makes
            #    adapt_hardware_objects() enumerate it: that loop iterates the dict
            #    keys and calls get_hardware_object(key), which prepends "/" and
            #    does a plain dict lookup -- a non-slash key would miss and be
            #    treated as a file to load, silently dropping the motor.
            #
            # NB: deliberately NOT added to self._hwobj_by_role. That dict is the
            # diffractometer's role map, walked by get_object_by_role() (e.g. for
            # "zoom"); leaking the minimal motor proxies into it breaks role
            # resolution. proxy.id does not need it -- it is computed from the
            # _hwobj_container chain -- and the adapter manager reads the global
            # hardware_objects list, not this role map.
            proxy._hwobj_container = self
            proxy._name = role
            HWR.get_hardware_repository().hardware_objects[
                f"/diffractometer/{role}"
            ] = proxy

            self.motors_hwobj_dict[role] = proxy
            setattr(self, role, proxy)

        if self.chan_state:
            self.current_state = self.chan_state.get_value()
            self.chan_state.connect_signal("update", self.state_changed)
        if self.chan_status:
            self.current_status = self.chan_status.get_value()
            self.chan_status.connect_signal("update", self.status_changed)

        self.chan_calib_x = self.get_channel_object("CoaxCamScaleX")
        self.chan_calib_y = self.get_channel_object("CoaxCamScaleY")
        self.update_pixels_per_mm()

        self.chan_head_type = self.get_channel_object("HeadType")
        if self.chan_head_type:
            try:
                self.head_type = DiffractometerHead(self.chan_head_type.get_value())
            except ValueError:
                self.head_type = DiffractometerHead.UNKNOWN

        self.chan_current_phase = self.get_channel_object("CurrentPhase")
        if self.chan_current_phase:
            self.chan_current_phase.connect_signal("update", self.current_phase_changed)
            self.current_phase_changed(self.chan_current_phase.get_value())

        self.chan_fast_shutter_is_open = self.get_channel_object("FastShutterIsOpen")
        if self.chan_fast_shutter_is_open:
            self.chan_fast_shutter_is_open.connect_signal(
                "update", self.fast_shutter_state_changed
            )

        self.chan_scintillator_position = self.get_channel_object(
            "ScintillatorPosition"
        )
        self.chan_capillary_position = self.get_channel_object("CapillaryPosition")

        self.cmd_start_set_phase = self.get_command_object("startSetPhase")
        self.cmd_start_auto_focus = self.get_command_object("startAutoFocus")
        self.cmd_get_omega_scan_limits = self.get_command_object(
            "getOmegaMotorDynamicScanLimits"
        )
        self.cmd_save_centring_positions = self.get_command_object(
            "saveCentringPositions"
        )

        for _role in self.config.nstate_equipment or ():
            _hobj = self.get_object_by_role(_role)
            if _hobj is not None:
                self.nstate_equipment_hwobj_dict[_role] = _hobj
                setattr(self, _role, _hobj)

        self.zoom = self.nstate_equipment_hwobj_dict.get(
            "zoom"
        ) or self.get_object_by_role("zoom")
        if self.zoom:
            self.connect(
                self.zoom, "valueChanged", self.zoom_position_changed
            )
            self.connect(
                self.zoom,
                "predefinedPositionChanged",
                self.zoom_motor_predefined_position_changed,
            )

        zoom_centre = self.get_property("zoom_centre")
        if isinstance(zoom_centre, str):
            zoom_centre = literal_eval(zoom_centre)
        if zoom_centre:
            self.zoom_centre = zoom_centre

        omega_ref = self.get_property("omega_reference") or {}
        if isinstance(omega_ref, str):
            omega_ref = literal_eval(omega_ref)
        self.omega_reference_par = omega_ref
        ref_role = omega_ref.get("motor_name") or omega_ref.get("actuator_name")
        if ref_role and ref_role in self.motors_hwobj_dict:
            self.omega_reference_motor = self.motors_hwobj_dict[ref_role]
            self.connect(
                self.omega_reference_motor,
                "valueChanged",
                self.omega_reference_motor_moved,
            )
            try:
                self.omega_reference_motor_moved(self.omega_reference_motor.get_value())
            except Exception:
                self.log.exception("Initial omega reference read failed")

        self.update_state(self.get_state())

    # ------------------------------------------------------------------
    # State / status / phases
    # ------------------------------------------------------------------

    def state_changed(self, state):
        if self.current_state != state:
            self.current_state = state
            self.emit("minidiffStateChanged", (self.current_state,))
        # Drive the framework state as well: `update_state` sets/clears the
        # ready event and emits `stateChanged`, which the web adapter consumes
        # for the Equipment READY/BUSY pill. Without this the diffractometer
        # stays stuck at its init state (UNKNOWN -> always BUSY).
        self.update_state(
            EXPORTER_TO_HWSTATE.get(state, HardwareObjectState.UNKNOWN)
        )

    def get_state(self):
        """Return the live MD2 state translated to a HardwareObjectState.

        Overrides the cached-``_state`` default so callers (and the init-time
        ``update_state(self.get_state())``) reflect the current MD2 ``State``
        channel instead of the value last written by ``update_state``.
        """
        if not self.chan_state:
            return HardwareObjectState.UNKNOWN
        return EXPORTER_TO_HWSTATE.get(
            self.chan_state.get_value(), HardwareObjectState.UNKNOWN
        )

    def status_changed(self, status):
        if self.current_status != status:
            self.current_status = status
            self.emit("minidiffStatusChanged", (self.current_status,))

    def current_phase_changed(self, raw_phase):
        phase_enum = self.PHASE_FROM_MD2.get(raw_phase, DiffractometerPhase.UNKNOWN)
        if self.current_phase == phase_enum:
            return
        self.current_phase = phase_enum
        if phase_enum != DiffractometerPhase.UNKNOWN:
            logging.getLogger("GUI").info(
                "Diffractometer: Current phase changed to %s", raw_phase
            )
        self.update_phase(phase_enum)
        self.emit("minidiffPhaseChanged", (raw_phase,))

    def set_phase(self, phase, timeout=60):
        """Set the MD2 phase.

        Accepts a ``DiffractometerPhase``, the enum name, or the raw MD2
        string (``Centring`` / ``DataCollection`` / ``BeamLocation`` /
        ``Transfer``). When entering or leaving Transfer/BeamLocation the
        detector is retracted to a safe distance first.
        """
        logging.getLogger("GUI").warning(
            "Diffractometer: Setting %s phase. Please wait...", phase
        )

        if isinstance(phase, DiffractometerPhase):
            phase_enum = phase
            md2_name = self.PHASE_TO_MD2[phase_enum]
        elif phase in self.PHASE_FROM_MD2:
            md2_name = phase
            phase_enum = self.PHASE_FROM_MD2[phase]
        else:
            phase_enum = self.value_to_enum(phase, DiffractometerPhase)
            md2_name = self.PHASE_TO_MD2.get(phase_enum, phase)

        protective = (DiffractometerPhase.TRANSFER, DiffractometerPhase.SEE_BEAM)
        if phase_enum in protective or self.current_phase in protective:
            detector_distance = HWR.beamline.detector.distance.get_value()
            self.log.debug(
                "Diffractometer current phase: %s selected phase: %s "
                "detector distance: %d mm",
                self.current_phase,
                md2_name,
                detector_distance,
            )
            if detector_distance < 350:
                logging.getLogger("GUI").info("Moving detector to safe distance")
                HWR.beamline.detector.distance.set_value(350)
                self.detector.insert_protective_cover()

        self.update_state(HardwareObjectState.BUSY)
        if timeout is not None:
            self.cmd_start_set_phase(md2_name)
            gevent.sleep(1)
            with gevent.Timeout(
                timeout, Exception(f"Timeout waiting for phase {md2_name}")
            ):
                while md2_name != self.chan_current_phase.get_value():
                    gevent.sleep(0.01)
        else:
            self.cmd_start_set_phase(md2_name)
        self.update_state(HardwareObjectState.READY)

    # ------------------------------------------------------------------
    # Helpers expected by SampleView and downstream callers
    # ------------------------------------------------------------------

    def get_pixels_per_mm(self):
        return self.pixels_per_mm_x, self.pixels_per_mm_y

    def update_pixels_per_mm(self, *args):
        if not (self.chan_calib_x and self.chan_calib_y):
            return
        try:
            calib_x = self.chan_calib_x.get_value()
            calib_y = self.chan_calib_y.get_value()
        except Exception as exc:
            logging.getLogger("HWR").warning(
                "PX2Diffractometer: pixels_per_mm read failed (%s); keeping previous values",
                exc,
            )
            return
        if not calib_x or not calib_y:
            logging.getLogger("HWR").warning(
                "PX2Diffractometer: pixels_per_mm calibration is zero/None "
                "(x=%r, y=%r); keeping previous values",
                calib_x,
                calib_y,
            )
            return
        self.pixels_per_mm_x = 1.0 / calib_x
        self.pixels_per_mm_y = 1.0 / calib_y
        self.emit(
            "pixelsPerMmChanged", ((self.pixels_per_mm_x, self.pixels_per_mm_y),)
        )

    def wait_status_ready(self, timeout=None):
        """Block until the MD2 application reports the global state Ready."""
        if not self.chan_state:
            return
        with gevent.Timeout(timeout, RuntimeError("Timeout waiting for MD2 ready")):
            while self.chan_state.get_value() != "Ready":
                gevent.sleep(0.05)

    def wait_device_ready(self, timeout=None):
        self.wait_status_ready(timeout)

    def get_status(self):
        if self.chan_status:
            self.current_status = self.chan_status.get_value()
        return self.current_status

    def is_sample_loaded(self):
        """Return whether a sample is currently mounted on the goniometer.

        Reads the MD Exporter ``SampleIsLoaded`` boolean declared in the
        diffractometer YAML. Used by the sample changer
        (``SOLEILCats.has_loaded_sample``) so the changer does not need its own
        connection to the goniometer.
        """
        chan = self.get_channel_object("SampleIsLoaded")
        if chan is None:
            self.log.warning(
                "PX2Diffractometer: SampleIsLoaded channel not configured"
            )
            return False
        # The Exporter returns booleans as the strings "true"/"false", so parse
        # them the same way the rest of the codebase does (str().lower()).
        return str(chan.get_value()).strip().lower() == "true"

    def use_sample_changer(self):
        return not self.in_plate_mode

    # ------------------------------------------------------------------
    # Backwards-compat shims for callers using the GenericDiffractometer
    # API names. AbstractDiffractometer renamed them to get_phase /
    # get_chip_configuration / in_plate_mode-as-property.
    # ------------------------------------------------------------------

    @property
    def in_plate_mode(self):
        return _CallableBool(self.head_type == DiffractometerHead.PLATE)

    def get_current_phase(self) -> str:
        phase = self.get_phase()
        return phase.name if phase else "Unknown"

    def get_head_configuration(self):
        return self.get_chip_configuration()

    def re_emit_values(self):
        if self.current_phase is not None:
            phase_name = self.PHASE_TO_MD2.get(self.current_phase, "Unknown")
            self.emit("minidiffPhaseChanged", (phase_name,))
        self.emit("omegaReferenceChanged", (self.reference_pos,))
        self.emit("minidiffShutterStateChanged", (self.fast_shutter_is_open,))

    def move_omega_relative(self, relative_angle, timeout=5):
        """Backwards-compat shim — prefer ``self.omega.set_value_relative``."""
        self.omega.set_value_relative(relative_angle, timeout)

    # ------------------------------------------------------------------
    # Beam / shutter
    # ------------------------------------------------------------------

    def fast_shutter_state_changed(self, is_open):
        self.fast_shutter_is_open = is_open
        self.emit("minidiffShutterStateChanged", (self.fast_shutter_is_open,))

    def toggle_fast_shutter(self):
        if self.chan_fast_shutter_is_open is not None:
            self.chan_fast_shutter_is_open.set_value(not self.fast_shutter_is_open)

    # ------------------------------------------------------------------
    # Zoom helpers
    # ------------------------------------------------------------------

    def zoom_position_changed(self, value):
        self.update_pixels_per_mm()
        self.refresh_omega_reference_position()

    def zoom_motor_predefined_position_changed(self, position_name, offset):
        self.update_pixels_per_mm()
        self.emit("zoomMotorPredefinedPositionChanged", (position_name, offset))

    def _step_zoom(self, delta):
        if not self.zoom:
            return
        levels = [v for v in self.zoom.VALUES if v.name != "UNKNOWN"]
        levels.sort(key=lambda v: v.value)
        current = self.zoom.get_value()
        try:
            idx = levels.index(current)
        except ValueError:
            idx = 0
        new_idx = max(0, min(len(levels) - 1, idx + delta))
        if levels[new_idx] is not current:
            self.zoom.set_value(levels[new_idx])

    def zoom_in(self):
        self._step_zoom(+1)

    def zoom_out(self):
        self._step_zoom(-1)

    def set_zoom(self, position):
        if not self.zoom:
            return
        if not isinstance(position, Enum):
            position = self.zoom.value_to_enum(position)
        self.zoom.set_value(position)

    # ------------------------------------------------------------------
    # Omega reference handling (overlay drawn on the SampleView)
    # ------------------------------------------------------------------

    def omega_reference_motor_moved(self, pos):
        if not self.omega_reference_par:
            return
        if self.omega_reference_par["camera_axis"].lower() == "x":
            pos = (
                self.omega_reference_par["direction"]
                * (pos - self.omega_reference_par["position"])
                * self.pixels_per_mm_x
                + self.zoom_centre["x"]
            )
            self.reference_pos = (pos, -10)
        else:
            pos = (
                self.omega_reference_par["direction"]
                * (pos - self.omega_reference_par["position"])
                * self.pixels_per_mm_y
                + self.zoom_centre["y"]
            )
            self.reference_pos = (-10, pos)
        self.emit("omegaReferenceChanged", (self.reference_pos,))

    def refresh_omega_reference_position(self):
        if self.omega_reference_motor is not None:
            self.omega_reference_motor_moved(self.omega_reference_motor.get_value())

    # ------------------------------------------------------------------
    # Auto-focus
    # ------------------------------------------------------------------

    def start_auto_focus(self, timeout=None):
        if self.cmd_start_auto_focus is None:
            return
        if timeout:
            self._ready_event.clear()
            gevent.spawn(self.cmd_start_auto_focus)
            self._ready_event.wait()
            self._ready_event.clear()
        else:
            self.cmd_start_auto_focus()

    # ------------------------------------------------------------------
    # Oscillation / scan limits
    # ------------------------------------------------------------------

    def get_osc_limits(self):
        return self.omega.get_dynamic_limits()

    def get_osc_max_speed(self):
        return self.omega.get_max_speed()

    def get_scan_limits(self, speed=None, num_images=None, exp_time=None):
        """Compute usable omega scan limits, accounting for acceleration."""
        if speed is not None:
            return self.cmd_get_omega_scan_limits(speed), None

        total_exposure_time = num_images * exp_time
        tmp = self.cmd_get_omega_scan_limits(0)
        max_speed = self.get_osc_max_speed()
        w0, w1 = tmp[0], tmp[1]

        x1, x2 = 10, 100
        c1 = self.cmd_get_omega_scan_limits(x1)[0] - w0
        c2 = self.cmd_get_omega_scan_limits(x2)[0] - w0
        a = -(c2 * x1 - c1 * x2) / (x1 * x2 * (x1 - x2))
        b = -(-c2 * pow(x1, 2) + c1 * pow(x2, 2)) / (x1 * x2 * (x1 - x2))

        result_speed = (
            -2 * b
            - total_exposure_time
            + sqrt((2 * b + total_exposure_time) ** 2 - 8 * a * (w0 - w1))
        ) / (4 * a)

        if result_speed < 0:
            return (None, None), None
        if result_speed > max_speed:
            delta = a * max_speed**2 + b * max_speed
            total_exposure_time = (w1 - w0 - 2 * delta) / (max_speed - 0.1)
        else:
            delta = a * result_speed**2 + b * result_speed
        return (w0 + delta, w1 - delta), total_exposure_time / num_images

    # ------------------------------------------------------------------
    # Scintillator and capillary
    # ------------------------------------------------------------------

    def get_scintillator_position(self):
        return self.chan_scintillator_position.get_value()

    def set_scintillator_position(self, position):
        self.chan_scintillator_position.set_value(position)
        with gevent.Timeout(5, Exception("Timeout waiting for scintillator position")):
            while position != self.get_scintillator_position():
                gevent.sleep(0.01)

    def get_capillary_position(self):
        return self.chan_capillary_position.get_value()

    def set_capillary_position(self, position):
        self.chan_capillary_position.set_value(position)
        with gevent.Timeout(5, Exception("Timeout waiting for capillary position")):
            while position != self.get_capillary_position():
                gevent.sleep(0.01)

    def save_centring_positions(self):
        self.cmd_save_centring_positions()

    # ------------------------------------------------------------------
    # SOLEIL diagnostic procedures
    # ------------------------------------------------------------------

    def beam_position_check(self):
        logging.getLogger("user_level_log").info("Going to check the beam position")
        self.bpc(wait=False)

    @task
    def bpc(self):
        ba = beam_align.beam_align(
            name_pattern=f"{os.getuid()}_{time.asctime().replace(' ', '_')}",
            directory=f"{os.getenv('HOME')}/beam_align",
        )
        log = logging.getLogger("user_level_log")
        log.info("Align beam to the optical centre of the camera")
        log.info("Moving scintillator to sample position, please wait ...")
        ba.execute()

        if ba.no_beam:
            log.info(ba.no_beam_message)
            return

        log.info(
            "Initial mirror positions (vfm, hfm) [mrad]: %.4f %.4f",
            *ba.initial_mirror_positions,
        )
        log.info(
            "Initial pixel shift from center (vertical, horizontal): %.1f, %.1f",
            *ba.initial_pixel_shift,
        )
        log.info(
            "Beam position adjustment finished after %d iterations",
            ba.number_of_iterations,
        )
        log.info(
            "Final mirror positions (vfm, hfm) [mrad]: %.4f %.4f",
            *ba.final_mirror_position,
        )
        log.info(
            "Final pixel shift from center (vertical, horizontal): %.1f, %.1f",
            *ba.final_pixel_shift,
        )
        log.info(
            "Delta in motor positions [mrad]: %.4f, %.4f",
            *(ba.final_mirror_position - ba.initial_mirror_positions),
        )

    @task
    def anneal(self, time=1.0):
        anneal_procedure(time)

    @task
    def excenter(
        self,
        scan_length=0.1,
        step=90.0,
        start=0.0,
        base_directory="/nfs/ruche/proxima2a-spool/2019_Run1/excenter",
        name_pattern="excenter",
    ):
        directory = os.path.join(
            base_directory, datetime.datetime.today().isoformat()
        )
        angles = str(tuple(np.arange(start, 360.0, step)))
        execute_line = (
            f"excenter.py -d {directory} -n {name_pattern} -l {scan_length:.2f} "
            f'-a "{angles}" &'
        )
        self.log.info("excenter angles %s", angles)
        self.log.info("excenter line %s", execute_line)
        os.system(execute_line)

    def aperture_align(self):
        logging.getLogger("user_level_log").info("Aligning the current aperture")
        self.aa(wait=False)

    @task
    def aa(self):
        log = logging.getLogger("user_level_log")
        log.info("Adjusting camera exposure time for visualisation on the scintillator")
        a = scan_and_align.scan_and_align("aperture", display=False)
        log.info("Scanning the aperture")
        a.scan()
        a.align(optimum="com")
        a.save_scan()
        log.info("Setting camera exposure time back to 0.050 seconds")
        log.info("Aligning aperture finished")
        a.predict()

    # ------------------------------------------------------------------
    # Kappa helpers
    # ------------------------------------------------------------------

    def close_kappa(self):
        gevent.spawn(self.close_kappa_task)

    def close_kappa_task(self):
        self.log.debug("Started closing Kappa")
        self.move_kappa_and_phi_procedure(0, None)
        self.wait_device_ready(60)
        self.motors_hwobj_dict["kappa"].home()
        self.wait_device_ready(60)
        self.move_kappa_and_phi_procedure(0, None)
        self.wait_device_ready(60)
        self.log.debug("Done closing Kappa")

    def move_kappa_and_phi(self, kappa=None, kappa_phi=None, wait=False):
        try:
            return self.move_kappa_and_phi_procedure(kappa, kappa_phi, wait=wait)
        except Exception:
            logging.exception("Could not move kappa and kappa_phi")

    @task
    def move_kappa_and_phi_procedure(self, new_kappa=None, new_kappa_phi=None):
        kappa = self.motors_hwobj_dict["kappa"].get_value()
        kappa_phi = self.motors_hwobj_dict["kappa_phi"].get_value()

        if new_kappa is None:
            new_kappa = kappa
        if new_kappa_phi is None:
            new_kappa_phi = kappa_phi

        if (kappa, kappa_phi) == (new_kappa, new_kappa_phi):
            return

        positions = {"kappa": new_kappa, "kappa_phi": new_kappa_phi}
        minikappa_correction = self.get_object_by_role("minikappa_correction")
        if minikappa_correction is not None:
            sampx = self.motors_hwobj_dict["sampx"].get_value()
            sampy = self.motors_hwobj_dict["sampy"].get_value()
            phiy = self.motors_hwobj_dict["phiy"].get_value()
            new_sampx, new_sampy, new_phiy = minikappa_correction.shift(
                kappa, kappa_phi, [sampx, sampy, phiy], new_kappa, new_kappa_phi
            )
            positions["sampx"] = new_sampx
            positions["sampy"] = new_sampy
            positions["phiy"] = new_phiy

        self.set_value_motors(positions, simultaneous=True, timeout=30)

    def visual_align(self, point_1, point_2):
        if self.in_plate_mode:
            self.log.info("PX2Diffractometer: Visual align not available in Plate mode")
            return
        t1 = [point_1.sampx, point_1.sampy, point_1.phiy]
        t2 = [point_2.sampx, point_2.sampy, point_2.phiy]
        kappa = self.motors_hwobj_dict["kappa"].get_value()
        phi = self.motors_hwobj_dict["kappa_phi"].get_value()
        new_kappa, new_phi, (new_sampx, new_sampy, new_phiy) = (
            self.goniometer.get_align_vector(t1, t2, kappa, phi)
        )
        self.set_value_motors(
            {
                "kappa": new_kappa,
                "kappa_phi": new_phi,
                "sampx": new_sampx,
                "sampy": new_sampy,
                "phiy": new_phiy,
            },
            simultaneous=True,
        )

    def get_point_from_line(self, point_one, point_two, frame_num, frame_total):
        """Linear interpolation between two centred points along a helical line."""
        new_point = {}
        point_one = point_one.as_dict()
        point_two = point_two.as_dict()
        for motor in point_one:
            new_point[motor] = point_one[motor] + (
                point_two[motor] - point_one[motor]
            ) * frame_num / float(frame_total)
        return new_point

    def set_collecting(self, collecting=True):
        self.collecting = collecting
