import os
import time
import logging

from mxcubecore.HardwareObjects.mockup.XRFSpectrumMockup import (
    XRFSpectrumMockup,
)
try:
    from fluorescence_spectrum import fluorescence_spectrum
except ModuleNotFoundError:
    from experimental_methods import fluorescence_spectrum
    
class PX2XRFSpectrum(XRFSpectrumMockup):
    def __init__(self, name):
        XRFSpectrumMockup.__init__(self, name)

        # read it from configuration
        self.config_filename = "/nfs/data2/Martin/Research/mxcube_2021/github/mxcubecore/mxcubecore/configuration/soleil/px2/production/experimental_methods/fit_configuration.cfg"

        self.log = logging.getLogger("user_level_log")

    def start_spectrum(
        self,
        count_time,
        spectrum_directory,
        archive_directory,
        prefix,
        session_id=None,
        blsample_id=None,
        adjust_transmission=True,
        excitation_energy=15.0,
    ):
        """
        Descript. :
        """

        archive_directory = spectrum_directory.replace("RAW_DATA", "ARCHIVE")

        for item in [
            ("count_time", count_time),
            ("excitation_energy", excitation_energy),
            ("spectrum_directory", spectrum_directory),
            ("archive_directory", archive_directory),
            ("prefix", prefix),
        ]:
            self.log.info("%s %s type %s" % (item[0], item[1], str(type(item[1]))))

        if not self.can_spectrum:
            self.spectrum_command_aborted()
            return False

        self.spectrum_info = {"sessionId": session_id, "blSampleId": blsample_id}
        if not os.path.isdir(archive_directory):
            logging.getLogger().debug(
                "XRFSpectrum: creating directory %s" % archive_directory
            )
            try:
                if not os.path.exists(archive_directory):
                    os.makedirs(archive_directory)
                if not os.path.exists(spectrum_directory):
                    os.makedirs(spectrum_directory)
            except OSError as diag:
                logging.getLogger().error(
                    "XRFSpectrum: error creating directory %s (%s)"
                    % (archive_directory, str(diag))
                )
                self.emit("xrfSpectrumStatusChanged", ("Error creating directory",))
                self.spectrum_command_aborted()
                return False
        archive_file_template = os.path.join(archive_directory, prefix)
        spectrum_file_template = os.path.join(spectrum_directory, prefix)
        if os.path.exists(archive_file_template + ".dat"):
            i = 1
            while os.path.exists(archive_file_template + "%d.dat" % i):
                i = i + 1
            archive_file_template += "_%d" % i
            spectrum_file_template += "_%d" % i
            prefix += "_%d" % i

        spectrum_file_dat_filename = os.path.extsep.join(
            (spectrum_file_template, "dat")
        )
        archive_file_dat_filename = os.path.extsep.join((archive_file_template, "dat"))
        archive_file_png_filename = os.path.extsep.join((archive_file_template, "png"))
        archive_file_html_filename = os.path.extsep.join(
            (archive_file_template, "html")
        )

        self.spectrum_info["filename"] = prefix
        self.spectrum_info["workingDirectory"] = archive_directory
        self.spectrum_info["scanFilePath"] = spectrum_file_dat_filename
        self.spectrum_info["scanFileFullPath"] = archive_file_dat_filename
        self.spectrum_info["jpegScanFileFullPath"] = r"%s" % archive_file_png_filename
        self.spectrum_info["exposureTime"] = count_time
        # self.spectrum_info["excitationEnergy"] = excitation_energy
        self.spectrum_info["annotatedPymcaXfeSpectrum"] = archive_file_html_filename
        self.spectrum_info["htmldir"] = archive_directory
        self.log.info("spectrum_info %s" % str(self.spectrum_info))
        self.spectrum_command_started()
        logging.getLogger().debug(
            "XRFSpectrum: spectrum dat file is %s", spectrum_file_dat_filename
        )
        logging.getLogger().debug(
            "XRFSpectrum: archive file is %s", archive_file_dat_filename
        )
        self.log.info(
            "PX2XRFSpectrum: fluorescence_spectrum parameters:\
                \n\tname_pattern: %s\
                \n\tdirectory: %s\
                \n\tintegration_time: %.2f\
                \n\texcitation_energy: %.2f"
            % (prefix, spectrum_directory, count_time, excitation_energy)
        )
        if excitation_energy < 1000:
            excitation_energy *= 1000.0
        self.experiment = fluorescence_spectrum(
            prefix,
            archive_directory,
            integration_time=count_time,
            photon_energy=excitation_energy,
            analysis=True,
            diagnostic=True,
            parent=self,
        )
        self.spectrum_command_started()

        self.experiment.execute()

        self.spectrum_data = self.experiment.spectrum
        self.mca_calib = self.experiment.get_calibration()[::-1]
        self.emit("progressStop", ())
        self.spectrum_command_finished()

    def stop(self):
        self.experiment.stop()
