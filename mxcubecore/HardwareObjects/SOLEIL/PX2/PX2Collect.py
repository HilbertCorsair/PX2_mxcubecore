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

import logging
from types import SimpleNamespace

import gevent
from helical_scan import helical_scan
from omega_scan import omega_scan

# from xray_centring import xray_centring
from raster_scan import raster_scan
from reference_images import reference_images
from slits import slits1

from mxcubecore import HardwareRepository as HWR
from mxcubecore.BaseHardwareObjects import HardwareObject
from mxcubecore.HardwareObjects.abstract.AbstractCollect import AbstractCollect
from mxcubecore.TaskUtils import task

__credits__ = ["Synchrotron SOLEIL"]
__version__ = "2.3."
__category__ = "General"


class PX2Collect(AbstractCollect, HardwareObject):
    """Main data collection class. Inherited from AbstractCollect.
    Collection is done by setting collection parameters and
    executing collect command
    """

    experiment_types = [
        "omega_scan",
        "reference_images",
        "inverse_scan",
        "mad",
        "helical_scan",
        "xrf_spectrum",
        "energy_scan",
        "raster_scan",
        "nested_helical_acquisition",
        "tomography",
        "film",
        "optical_centering",
    ]

    # experiment_types = ['OSC',
    # 'Collect - Multiwedge',
    # 'Helical',
    # 'Mesh',
    # 'energy_scan',
    # 'xrf_spectrum',
    # 'neha'

    def __init__(self, name):
        """
        :param name: name of the object
        :type name: string
        """

        AbstractCollect.__init__(self, name)
        HardwareObject.__init__(self, name)

        self.current_dc_parameters = None
        self.osc_id = None
        self.owner = None
        self.aborted_by_user = None
        self.slits1 = slits1()

    def init(self):
        self.ready_event = gevent.event.Event()

        undulators = self._normalise_undulators(self.get_property("undulators", []))
        beam_div_hor, beam_div_ver = HWR.beamline.beam.get_beam_divergence()

        self.set_beamline_configuration(
            synchrotron_name="SOLEIL",
            directory_prefix=self.get_property("directory_prefix"),
            default_exposure_time=HWR.beamline.detector.get_property(
                "default_exposure_time"
            ),
            minimum_exposure_time=HWR.beamline.detector.get_property(
                "minimum_exposure_time"
            ),
            detector_fileext=HWR.beamline.detector.get_property("fileSuffix"),
            detector_type=HWR.beamline.detector.get_property("type"),
            detector_manufacturer=HWR.beamline.detector.get_property("manufacturer"),
            detector_model=HWR.beamline.detector.get_property("model"),
            detector_px=HWR.beamline.detector.get_property("px"),
            detector_py=HWR.beamline.detector.get_property("py"),
            detector_binning_mode=HWR.beamline.detector.get_binning_mode(),
            undulators=undulators,
            focusing_optic=self.get_property("focusing_optic"),
            monochromator_type=self.get_property("monochromator"),
            beam_divergence_vertical=beam_div_ver,
            beam_divergence_horizontal=beam_div_hor,
            polarisation=self.get_property("polarisation"),
            input_files_server=self.get_property("input_files_server"),
        )

        self.emit("collectConnected", (True,))
        self.emit("collectReady", (True,))

    @staticmethod
    def _normalise_undulators(undulators):
        """Return undulators as objects with a `type` attribute.

        The yaml gives a list of dicts, but LIMS reporting walks
        `bl_config.undulators` reading `.type`.
        """
        normalised = []

        for undulator in undulators or ():
            if isinstance(undulator, dict):
                # {"undulator": {"type": "U24"}} or {"type": "U24"}
                config = undulator.get("undulator", undulator)
                normalised.append(SimpleNamespace(**config))
            else:
                normalised.append(undulator)

        return normalised

    def data_collection_hook(self):
        """Main collection hook"""

        if self.aborted_by_user:
            self.collection_failed("Aborted by user")
            self.aborted_by_user = False
            return

        parameters = self.current_dc_parameters

        log = logging.getLogger("user_level_info")
        log.info("data collection parameters received %s" % parameters)

        for parameter in parameters:
            log.info("%s: %s" % (str(parameter), str(parameters[parameter])))

        osc_seq = parameters["oscillation_sequence"][0]
        fileinfo = parameters["fileinfo"]
        sample_reference = parameters["sample_reference"]
        experiment_type = parameters["experiment_type"]
        energy = parameters["energy"]
        transmission = parameters["transmission"]
        resolution = parameters["resolution"]

        exposure_time = osc_seq["exposure_time"]
        in_queue = parameters["in_queue"] != False

        overlap = osc_seq["overlap"]
        angle_per_frame = osc_seq["range"]
        scan_start_angle = osc_seq["start"]
        number_of_images = osc_seq["number_of_images"]
        image_nr_start = osc_seq["start_image_number"]

        directory = fileinfo["directory"]
        prefix = fileinfo["prefix"]
        template = fileinfo["template"]
        run_number = fileinfo["run_number"]
        process_directory = fileinfo["process_directory"]

        # space_group = str(sample_reference['space_group'])
        # unit_cell = list(eval(sample_reference['cell']))

        self.emit("collectStarted", (self.owner, 1))
        self.emit("progressInit", ("Data collection", 100))
        self.emit("fsmConditionChanged", "data_collection_started", True)

        self.store_image_in_lims_by_frame_num(1)

        # The template carries the frame placeholder ("prefix_1_%06d.h5"), the
        # experiments want the bare name ("prefix_1").
        name_pattern = template.split("%")[0].rstrip("_")

        # Centred position, in MD2 motor names. The experiments fall back to
        # the current goniometer position when this is None.
        position = self.translate_position(parameters.get("motors") or {}) or None

        try:
            if experiment_type == "OSC":
                scan_range = angle_per_frame * number_of_images
                scan_exposure_time = exposure_time * number_of_images
                experiment = omega_scan(
                    name_pattern,
                    directory,
                    scan_range=scan_range,
                    scan_exposure_time=scan_exposure_time,
                    scan_start_angle=scan_start_angle,
                    angle_per_frame=angle_per_frame,
                    image_nr_start=image_nr_start,
                    position=position,
                    photon_energy=energy,
                    transmission=transmission,
                    resolution=resolution,
                    simulation=False,
                )

            elif experiment_type == "Characterization":
                number_of_wedges = osc_seq["number_of_images"]
                # osc_seq carries no wedge_size: one frame per wedge unless
                # configured otherwise.
                wedge_size = osc_seq.get("wedge_size") or self.get_property(
                    "reference_wedge_size", 1
                )
                overlap = osc_seq["overlap"]
                scan_start_angles = []
                scan_exposure_time = exposure_time * wedge_size
                scan_range = angle_per_frame * wedge_size

                for k in range(number_of_wedges):
                    scan_start_angles.append(
                        scan_start_angle + k * -overlap + k * scan_range
                    )

                experiment = reference_images(
                    name_pattern,
                    directory,
                    scan_range=scan_range,
                    scan_exposure_time=scan_exposure_time,
                    scan_start_angles=scan_start_angles,
                    angle_per_frame=angle_per_frame,
                    image_nr_start=image_nr_start,
                    position=position,
                    photon_energy=energy,
                    transmission=transmission,
                    resolution=resolution,
                    simulation=False,
                )

            elif experiment_type == "Helical" and osc_seq["mesh_range"] == ():
                scan_range = angle_per_frame * number_of_images
                scan_exposure_time = exposure_time * number_of_images
                log.info("helical_pos %s" % self.helical_pos)
                experiment = helical_scan(
                    name_pattern,
                    directory,
                    scan_range=scan_range,
                    scan_exposure_time=scan_exposure_time,
                    scan_start_angle=scan_start_angle,
                    angle_per_frame=angle_per_frame,
                    image_nr_start=image_nr_start,
                    position_start=self.translate_position(self.helical_pos["1"]),
                    position_end=self.translate_position(self.helical_pos["2"]),
                    photon_energy=energy,
                    transmission=transmission,
                    resolution=resolution,
                    simulation=False,
                )

            elif experiment_type == "Helical" and osc_seq["mesh_range"] != ():
                # X-ray centring is not ported to PX2 yet: the xray_centring
                # import is commented out at the top of this file.
                raise RuntimeError(
                    "X-ray centring (helical over a grid) is not supported at PX2"
                )

            elif experiment_type == "Mesh":
                number_of_columns = osc_seq["number_of_lines"]
                number_of_rows = int(number_of_images / number_of_columns)
                horizontal_range, vertical_range = osc_seq["mesh_range"]
                angle_per_line = angle_per_frame * number_of_columns
                experiment = raster_scan(
                    name_pattern,
                    directory,
                    vertical_range,
                    horizontal_range,
                    number_of_rows,
                    number_of_columns,
                    frame_time=exposure_time,
                    scan_start_angle=scan_start_angle,
                    scan_range=angle_per_line,
                    image_nr_start=image_nr_start,
                    position=position,
                    photon_energy=energy,
                    transmission=transmission,
                    simulation=False,
                )

            else:
                raise RuntimeError(
                    "Unsupported experiment type '%s'" % experiment_type
                )

            experiment.execute()
        except RuntimeError:
            raise
        except Exception as ex:
            # do_collect() only handles RuntimeError; anything else escaping
            # from here never sets ready_event and hangs the queue.
            raise RuntimeError(
                "%s failed: %s: %s" % (experiment_type, type(ex).__name__, ex)
            ) from ex

        # for image in range(number_of_images):
        # if self.aborted_by_user:
        # self.ready_event.set()
        # return

        # Uncomment to test collection failed
        # if image == 5:
        # self.emit("collectOscillationFailed", (self.owner, False,
        # "Failed on 5", parameters.get("collection_id")))
        # self.ready_event.set()
        # return

        # gevent.sleep(exposure_time)
        # self.emit("collectImageTaken", image)
        # self.emit("progressStep", (int(float(image) / number_of_images * 100)))

        # NB do not finish the collection here: do_collect() updates LIMS and
        # calls collection_finished() (which sets ready_event) once the hook
        # returns.

    def translate_position(self, position):
        """Translate mxcube motor roles to the MD2 names the experiments use."""
        # Single source of truth for the mapping (phiy IS AlignmentY, and PX2
        # has focus, not phix).
        translation = getattr(
            HWR.beamline.diffractometer, "MOTOR_ROLE_TO_MD2", {}
        )
        translated_position = {}

        for key, value in (position or {}).items():
            if value is None:
                continue
            # The scan angles are given separately; a position holding Omega
            # would fight the scan start angle.
            if key in ("omega", "Omega"):
                continue
            translated_position[translation.get(key, key)] = value

        return translated_position

    def trigger_auto_processing(self, process_event, frame_number):
        """
        Descript. :
        """
        if HWR.beamline.offline_processing is not None:
            HWR.beamline.offline_processing.execute_autoprocessing(
                process_event,
                self.current_dc_parameters,
                frame_number,
                self.run_offline_processing,
            )

    def update_data_collection_in_lims(self):
        """Collect LIMS metadata only when there is a LIMS to store it in.

        The base implementation only checks that the object exists, and then
        gathers values that raise without a connection. Any such error escapes
        do_collect() (it only handles RuntimeError) and hangs the queue.
        """
        lims = HWR.beamline.lims

        if not lims or not lims.is_connected():
            return

        super(PX2Collect, self).update_data_collection_in_lims()

    @task
    def _take_crystal_snapshot(self, filename):
        HWR.beamline.sample_view.save_snapshot(filename)

    @task
    def _take_crystal_animation(self, animation_filename, duration_sec):
        """Rotates sample by 360 and composes a gif file
        Animation is saved as the fourth snapshot
        """
        HWR.beamline.sample_view.save_scene_animation(animation_filename, duration_sec)

    @task
    def move_motors(self, motor_position_dict):
        """Move the goniometer to the centred position of the collection."""
        positions = {
            role: value
            for role, value in (motor_position_dict or {}).items()
            if value is not None
        }

        if not positions:
            return

        logging.getLogger("user_level_log").info(
            "Collection: moving to centred position %s", positions
        )
        HWR.beamline.diffractometer.set_value_motors(
            positions, timeout=self.get_property("move_timeout", 30)
        )

    def store_image_in_lims_by_frame_num(self, frame_number):
        """
        Descript. :
        """
        self.trigger_auto_processing("image", frame_number)

        lims = HWR.beamline.lims

        if not lims or not lims.is_connected():
            return None

        return self.store_image_in_lims(frame_number)

    def stop_collect(self):
        """Abort the collection, on the hardware as well as in the queue."""
        self.aborted_by_user = True

        try:
            HWR.beamline.diffractometer.abort()
        except Exception:
            self.log.exception("Collection: could not abort the goniometer")

        super(PX2Collect, self).stop_collect()

    def set_helical_pos(self, helical_pos):
        self.helical_pos = helical_pos

    def get_slit_gaps(self):
        return self.get_slits_gap()

    def get_slits_gap(self):
        return self.slits1.get_horizontal_gap(), self.slits1.get_vertical_gap()
