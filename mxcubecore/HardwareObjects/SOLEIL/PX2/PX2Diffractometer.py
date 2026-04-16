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
from math import sqrt

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
from mxcubecore.Command.Exporter import Exporter
from mxcubecore.HardwareObjects.abstract.AbstractDiffractometer import (
    AbstractDiffractometer,
    DiffractometerHead,
    DiffractometerPhase,
)
from mxcubecore.HardwareObjects.abstract.AbstractMotor import AbstractMotor
from mxcubecore.TaskUtils import task

__credits__ = ["SOLEIL"]
__version__ = "3.0"
__category__ = "General"


class MD2MotorProxy(AbstractMotor):
    """Lightweight in-process motor backed by the MD2 Exporter.

    Built and ``init()``-d directly by ``PX2Diffractometer`` rather than
    loaded by the YAML loader, so the per-motor wrapper YAML files are no
    longer needed. All proxies share the diffractometer's single Exporter
    connection.
    """

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
        return self._exporter.read_property(self._position_event)

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

    def get_limits(self):
        try:
            low, high = self._exporter.execute(
                "getMotorLimits", (self.actuator_name,)
            )
            return float(low), float(high)
        except Exception:
            return self._nominal_limits

    def get_dynamic_limits(self):
        try:
            low, high = self._exporter.execute(
                "getMotorDynamicLimits", (self.actuator_name,)
            )
            return float(low), float(high)
        except Exception:
            return (-1e4, 1e4)

    def get_max_speed(self):
        return self._exporter.execute("getMotorMaxSpeed", (self.actuator_name,))

    def home(self, timeout=None):
        self._exporter.execute("startHomingMotor", (self.actuator_name,))
        self.wait_ready(timeout)


class PX2Diffractometer(AbstractDiffractometer):
    """SOLEIL Proxima 2A MD2 diffractometer."""

    # Convention naming: omega for the rotation axis, phiz/phiy for alignment.
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

    def __init__(self, name):
        super().__init__(name)
        self._exporter = None

        self.zoom_motor_hwobj = None
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

        exporter_address = self.get_property("exporter_address")
        if not exporter_address:
            raise RuntimeError("PX2Diffractometer: 'exporter_address' missing")
        host, port = exporter_address.split(":")
        self._exporter = Exporter(host, int(port))

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
            self.motors_hwobj_dict[role] = proxy
            setattr(self, role, proxy)

        for role in self.config.nstate_equipment or []:
            obj = self.get_object_by_role(role)
            if obj is None:
                self.log.warning(
                    "PX2Diffractometer: no nstate object for role '%s'", role
                )
                continue
            self.nstate_equipment_hwobj_dict[role] = obj
            setattr(self, role, obj)
            self.connect(obj, "valueChanged", obj.update_value)

        self.chan_state = self.get_channel_object("State")
        self.chan_status = self.get_channel_object("Status")
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

        self.zoom_motor_hwobj = self.nstate_equipment_hwobj_dict.get(
            "zoom"
        ) or self.get_object_by_role("zoom")
        if self.zoom_motor_hwobj:
            self.connect(
                self.zoom_motor_hwobj, "valueChanged", self.zoom_position_changed
            )
            self.connect(
                self.zoom_motor_hwobj,
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
        if self.chan_calib_x and self.chan_calib_y:
            self.pixels_per_mm_x = 1.0 / self.chan_calib_x.get_value()
            self.pixels_per_mm_y = 1.0 / self.chan_calib_y.get_value()
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

    def use_sample_changer(self):
        return not self.in_plate_mode

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
        msg = "Opened" if is_open else "Closed"
        self.emit("minidiffShutterStateChanged", (self.fast_shutter_is_open, msg))

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

    def zoom_in(self):
        if self.zoom_motor_hwobj:
            self.zoom_motor_hwobj.zoom_in()

    def zoom_out(self):
        if self.zoom_motor_hwobj:
            self.zoom_motor_hwobj.zoom_out()

    def set_zoom(self, position):
        if self.zoom_motor_hwobj:
            self.zoom_motor_hwobj.moveToPosition(position)

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
