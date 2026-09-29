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

"""Characterisation hardware object for PROXIMA 2A.

Strategy calculation is still mocked: PX2 has no EDNA installation, so
``characterise()`` returns the canned result of EDNACharacterisationMockup and
the resulting diffraction plan is only proposed, never queued
(``auto_add_diff_plan: false`` in diffraction_methods.yaml).

What this object adds over the plain mockup is the PX2 configuration: the EDNA
defaults are kept as YAML (``edna_defaults.yaml``) like the rest of the PX2
config, while the upstream object expects the XSData XML serialisation. The
YAML mirrors the XML element tree one to one, so it is converted back to XML
here and the inherited parsing (and
``get_default_characterisation_parameters()``) works unchanged.
"""

import os
from xml.etree import ElementTree

from ruamel.yaml import YAML

from mxcubecore import HardwareRepository as HWR
from mxcubecore.HardwareObjects.mockup.EDNACharacterisationMockup import (
    EDNACharacterisationMockup,
)

__credits__ = ["Synchrotron SOLEIL"]
__license__ = "LGPLv3"


class PX2EDNACharacterisation(EDNACharacterisationMockup):
    def init(self) -> None:
        self.start_edna_command = self.get_property("edna_command")
        self.edna_default_file = self.get_property("edna_default_file")

        file_path = HWR.get_hardware_repository().find_in_repository(
            self.edna_default_file
        )

        if file_path is None:
            file_path = self.edna_default_file

            if not os.path.exists(file_path):
                raise ValueError(
                    "File %s not found in repository" % self.edna_default_file
                )

        if os.path.splitext(file_path)[1].lower() in (".yaml", ".yml"):
            self.edna_default_input = self._xml_from_yaml(file_path)
        else:
            with open(file_path, "r") as fp:
                self.edna_default_input = fp.read()

    @classmethod
    def _xml_from_yaml(cls, file_path: str) -> str:
        """Serialise an XSData YAML file to the XML string XSData can parse."""
        with open(file_path, "r") as fp:
            content = YAML(typ="safe", pure=True).load(fp)

        if not isinstance(content, dict) or len(content) != 1:
            raise ValueError(
                "%s: expected a single XSData root element, got %s"
                % (file_path, type(content).__name__)
            )

        root_name, root_content = next(iter(content.items()))
        root = ElementTree.Element(root_name)

        if isinstance(root_content, dict):
            for key, value in root_content.items():
                cls._add_element(root, key, value)

        return ElementTree.tostring(root, encoding="unicode")

    @classmethod
    def _add_element(cls, parent, key, value) -> None:
        # A null value means "not set in the defaults" (e.g. dataSet.imageFile,
        # which is filled in per collection): leave the element out entirely.
        if value is None:
            return

        if isinstance(value, list):
            for item in value:
                cls._add_element(parent, key, item)
            return

        child = ElementTree.SubElement(parent, key)

        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                cls._add_element(child, sub_key, sub_value)
        else:
            child.text = str(value)
