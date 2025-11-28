#
#  Project: MXCuBE
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

"""

[Name] SOLEILMachineInfo
based on EMBLMachineInfo

[Description]
Hardware Object is used to get relevant machine information
(current, intensity, hutch temperature and humidity, and data storage disc
information). Value limits are included

[Channels]
- chanMachCurr
- chanStateText

[Commands]
- cmdSetIntensResolution
- cmdSetIntensAcqTime
- cmdSetIntensRange

[Emited signals]
- valuesChanged
- inRangeChanged

[Functions]
- mach_current_changed()
- machStateTextChanged()
- updateValues()
- setInitialIntens()
- setExternalValues()


[Included Hardware Objects]

Example Hardware Object XML file :
==================================
<device class="MachineInfo">
    <updateIntervalS>120</updateIntervalS>
    <discPath>/home</discPath>
    <limits>{'current':90, 'temp': 25, 'hum': 60, 'intens': 0.1,
             'discSizeGB': 20}</limits>
</device>
"""
import os
import time
import logging

from mxcubecore.BaseHardwareObjects import HardwareObject

from mxcubecore import HardwareRepository as HWR

__credits__ = ["SOLEIL", "EMBL Hamburg"]
__version__ = "2.3."
__category__ = "General"

try:
    from flux import flux, flux_mockup
except ModuleNotFoundError:
    from experimental_methods import flux, flux_mockup
    
class SOLEILMachineInfo(HardwareObject):
    """
    Descript. : Displays actual information about the beeamline
    """

    def __init__(self, name):
        """__init__"""
        HardwareObject.__init__(self, name)
        # Parameters
        self.update_interval = None
        self.limits_dict = None
        self.hutch_temp_addr = None
        self.hutch_hum_addr = None
        self.hutch_temp = 0
        self.hutch_hum = 0
        self.overflow_alarm = None
        self.low_level_alarm = 0
        self.auto_refill = None
        self.state_text = "Not updated yet"
        self.ring_energy = None
        self.bunch_count = None
        self.flux_area = None
        self.last_transmission = None

        self.flux_monitor = flux()
        self.flux_monitor_mockup = flux_mockup()
        
        self.values_list = []
        # Intensity current ranges
        # Machine current, index = 0
        temp_dict = {}
        temp_dict["value"] = 0
        temp_dict["value_str"] = ""
        temp_dict["in_range"] = False
        temp_dict["title"] = "Machine current"
        temp_dict["bold"] = True
        temp_dict["font"] = 14
        # temp_dict['history'] = True
        self.values_list.append(temp_dict)

        # Machine state, index = 1
        temp_dict = {}
        temp_dict["value"] = None
        temp_dict["in_range"] = True
        temp_dict["title"] = "Machine state"
        self.values_list.append(temp_dict)

        # Hutch temperature, index = 2
        temp_dict = {}
        temp_dict["value"] = ""
        temp_dict["value_str"] = ""
        temp_dict["in_range"] = None
        temp_dict["title"] = "Hutch temperature"
        self.values_list.append(temp_dict)

        # Flux, index = 3
        temp_dict = {}
        temp_dict["value"] = 1
        temp_dict["value_str"] = "%.2e ph/s" % self.get_flux() #HWR.beamline.flux.get_value()
        temp_dict["in_range"] = None
        temp_dict["title"] = "Flux"
        temp_dict["align"] = "center"
        self.values_list.append(temp_dict)
        
        # Beam size, index = 4
        temp_dict = {}
        temp_dict['value'] = (0.01, 0.005)
        temp_dict['value_str'] = "%.3fx%.3f mm" % (0.01, 0.005)
        temp_dict['in_range'] = True
        temp_dict['title'] = "Beam size"
        #temp_dict['align'] = "left"
        self.values_list.append(temp_dict)
        
        # Cryo jet, index = 5
        temp_dict = {}
        temp_dict["value"] = "???"
        temp_dict["in_range"] = None
        temp_dict["title"] = "Cryostream"
        self.values_list.append(temp_dict)

        # Dewar level, index = 6
        temp_dict = {}
        temp_dict["value"] = "Dewar level in range"
        temp_dict["in_range"] = True
        temp_dict["title"] = "Sample changer"
        self.values_list.append(temp_dict)
        
        # Dewar level, index = 7
        temp_dict = {}
        temp_dict["value"] = "???"
        temp_dict["in_range"] = True
        temp_dict["title"] = "Ramdisk"
        self.values_list.append(temp_dict)


        self.temp_hum_values = [None, None]
        self.temp_hum_in_range = [None, None]
        self.temp_hum_polling = None

        self.chan_flux = None
        self.chan_energy = None
        self.chan_mach_curr = None
        self.chan_mach_energy = None
        self.chan_bunch_count = None
        self.chan_state_text = None
        self.chan_cryojet_in = None
        self.chan_sample_temperature = None
        self.chan_sc_dewar_low_level_alarm = None
        self.chan_sc_dewar_overflow_alarm = None

        self.ring_energy = 2.75
        
    def init(self):
        """init"""
        self.update_interval = int(self.get_property("updateIntervalS"))
        self.limits_dict = eval(self.get_property("limits"))

        self.chan_mach_curr = self.get_channel_object("machCurrent")
        if self.chan_mach_curr is not None:
            self.chan_mach_curr.connect_signal("update", self.mach_current_changed)

        self.chan_filling_mode = self.get_channel_object("fillingMode")
        if self.chan_filling_mode is not None:
            self.chan_filling_mode.connect_signal("update", self.state_text_changed)

        self.chan_state_text0 = self.get_channel_object("operatorMessage0")
        if self.chan_state_text0 is not None:
            self.chan_state_text0.connect_signal("update", self.state_text_changed)

        self.chan_state_text1 = self.get_channel_object("operatorMessage1")
        if self.chan_state_text1 is not None:
            self.chan_state_text1.connect_signal("update", self.state_text_changed)

        self.chan_state_text2 = self.get_channel_object("operatorMessage2")
        if self.chan_state_text2 is not None:
            self.chan_state_text2.connect_signal("update", self.state_text_changed)

        self.chan_is_beam_usable = self.get_channel_object("isBeamUsable")
        if self.chan_is_beam_usable is not None:
            self.chan_is_beam_usable.connect_signal("update", self.state_text_changed)

        self.chan_cryojet_in = self.get_channel_object("cryojetIn")
        if self.chan_cryojet_in is not None:
            self.cryojet_in_changed(self.chan_cryojet_in.get_value())
            self.chan_cryojet_in.connect_signal("update", self.cryojet_in_changed)
        else:
            logging.getLogger("HWR").debug("MachineInfo: Cryojet channel not defined")

        if self.chan_is_beam_usable is not None or self.chan_filling_mode is not None:
            self.state_text_changed()
            
        self.chan_sample_temperature = self.get_channel_object("sampleTemp")
        if self.chan_sample_temperature is not None:
            self.chan_sample_temperature.connect_signal('update', self.cryojet_in_changed)
            self.cryojet_in_changed(self.chan_cryojet_in.get_value())

        self.chan_sc_auto_refill = self.get_channel_object("scAutoRefill")
        if self.chan_sc_auto_refill is not None:
            self.chan_sc_auto_refill.connect_signal(
                "update", self.sc_autorefill_changed
            )
            self.sc_autorefill_changed(self.chan_sc_auto_refill.get_value())

        self.chan_sc_dewar_low_level_alarm = self.get_channel_object("scLowLevelAlarm")
        if self.chan_sc_dewar_low_level_alarm is not None:
            self.chan_sc_dewar_low_level_alarm.connect_signal(
                "update", self.low_level_alarm_changed
            )
            self.low_level_alarm_changed(self.chan_sc_dewar_low_level_alarm.get_value())

        self.chan_sc_dewar_overflow_alarm = self.get_channel_object("scOverflowAlarm")
        if self.chan_sc_dewar_overflow_alarm is not None:
            self.chan_sc_dewar_overflow_alarm.connect_signal(
                "update", self.overflow_alarm_changed
            )

        self.chan_flux = self.get_channel_object('transmission')
        if self.chan_flux is not None:
            self.chan_flux.connect_signal('update', self.flux_changed)

        self.chan_energy = self.get_channel_object('energy')
        logging.getLogger('HWR').debug('self.chan_energy %s' % self.chan_energy)
        if self.chan_energy is not None:
            self.chan_energy.connect_signal('update', self.energy_changed)
            self.flux_changed(-1) #self.flux.get_flux())
            
        self.chan_temperature_exp = self.get_channel_object("temperatureExp")
        if self.chan_temperature_exp is not None:
            try:
                self.chan_temperature_exp.connect_signal("update", self.temperature_changed)
                self.temperature_changed(self.chan_temperature_exp.get_value())
            except:
                self.chan_temperature_exp = None
                
        self.re_emit_values()

    def clear_gevent(self):
        self.temp_hum_polling.kill()
        if self.update_task:
            self.update_task.kill()

    def cryojet_in_changed(self, value):
        """Cryojet in/out value changed"""
        try:
            if self.chan_cryojet_in is not None:
                in_value = self.chan_cryojet_in.get_value()
            else:
                in_value = -1
            if self.chan_sample_temperature is not None:
                temp_value = self.chan_sample_temperature.get_value()
            else:
                temp_value = -1
            #logging.getLogger("HWR").debug("cryojet_in_changed: %s %s" % (in_value, temp_value))
            self.values_list[5]["in_range"] = False
            self.values_list[5]["bold"] = True

            if in_value == 0:
                self.values_list[5]["value"] = " In place"
                self.values_list[5]["in_range"] = True
                self.values_list[5]["bold"] = False
            elif temp_value == 1:
                self.values_list[5]["value"] = "NOT IN PLACE"
            else:
                self.values_list[5]["value"] = "Unknown"

            if self.chan_sample_temperature is not None:
                self.values_list[5]["value"] += (
                    "\n sample temperature: %.1f K"
                    % temp_value
                )
            else:
                pass
                #logging.getLogger("HWR").debug(
                    #"chan_sample_temperature: %s" % self.chan_sample_temperature
                #)
            self.re_emit_values()
        except:
            pass
        
    def mach_current_changed(self, value):
        """Method called if the machine current is changed

        :param value: new machine current
        :type value: float
        """
        if (
            self.values_list[0]["value"] is None
            or abs(self.values_list[0]["value"] - value) > 0.00001
        ):
            self.values_list[0]["value"] = value
            self.values_list[0]["value_str"] = "%.1f mA" % value
            self.values_list[0]["in_range"] = value > 10.0
            self.re_emit_values()

    def state_text_changed(self, text=None):
        """Function called if machine state text is changed

        :param text: new machine state text
        :type text: string
        """
        # self.state_text = str(text)
        # self.values_list[1]['in_range'] = text != "Fehler"
        self.update_machine_state()

    def update_machine_state(self):
        """Machine state assembly"""
        logging.getLogger('HWR').debug('update_machine_state')
        filling_mode = self.chan_filling_mode.get_value()
        state_text0 = self.chan_state_text0.get_value()
        state_text1 = self.chan_state_text1.get_value()
        state_text2 = self.chan_state_text2.get_value()
        is_beam_usable = self.chan_is_beam_usable.get_value()
        
        date_boundary_string = ' :'
        date_boundary = state_text1.find(date_boundary_string)
        date = state_text1[:date_boundary]
        state_text1 = state_text1[date_boundary + len(date_boundary_string):]
        
        state_text = "%s\n%s\n" % (date, state_text0)
        
        line_length = 26
        
        #state_text += "electron energy: %.2f GeV\n" % (self.ring_energy,)
        state_text += "filling: %s\n" % (filling_mode,)
        
        if state_text1 != ' ' and state_text1 != '' and state_text1.count(' ') != len(state_text1):
            st1 = state_text1.split()
            line = ''
            while st1:
                word = st1.pop(0)
                if len(line + word) < line_length:
                    if line == '':
                        line = word
                    else:
                        line = '%s %s' % (line, word)
                else:
                    state_text += "%s\n" % (line, )
                    line = word
            if line != '':
                state_text += "%s\n" % (line, )
                #state_text += "%s\n" % (state_text1, )
          
        st2 = state_text2.split()
        line = ''
        while st2:
            word = st2.pop(0)
            if len(line + word) < line_length:
                if line == '':
                    line = word
                else:
                    line = '%s %s' % (line, word)
            else:
                state_text += "%s\n" % (line, )
                line = word
        if line != '':
            state_text += "%s\n" % (line, )
        
        if is_beam_usable:
            self.values_list[1]['in_range'] = True
            state_text += 'Beam usable'
        else:
            self.values_list[1]['in_range'] = False
            state_text += 'Beam unusable'
        self.values_list[1]['value'] = state_text
        self.state_text = state_text
        logging.getLogger().debug('update_machine_state')
        self.update_ramdisk_size()
        self.re_emit_values()

    def low_level_alarm_changed(self, value):
        """Low level alarm"""
        self.low_level_alarm = value
        self.update_sc_alarm()

    def overflow_alarm_changed(self, value):
        """Overflow alarm"""
        self.overflow_alarm = value
        self.update_sc_alarm()

    def sc_autorefill_changed(self, value):
        self.auto_refill = value
        self.update_sc_alarm()

    def file_transfer_status_changed(self, total, pending, failed):
        self.values_list[-1]["value"] = "%d  -  %d  -  %d" % (total, pending, failed)
        self.values_list[-1]["in_range"] = failed == 0

        if failed > 0:
            logging.getLogger("GUI").error(
                "Error in file transfer (%d files failed to copy)." % failed
            )

    def update_sc_alarm(self):
        """Sample changer alarm"""
        logging.getLogger('HWR').debug('update_sc_alarm low_level_alarm %s overflow_alarm %s ' % (self.low_level_alarm, self.overflow_alarm))
        if self.low_level_alarm in [1, True, None] and self.overflow_alarm in [1, True, None]:
            self.values_list[6]["value"] = "Dewar level in range"
            self.values_list[6]["in_range"] = True
            logging.getLogger("GUI").debug("Liquid nitrogen " + \
            "in range")
        elif self.low_level_alarm not in [1, True, None]:
            self.values_list[6]["value"] = "Low level alarm!"
            self.values_list[6]["in_range"] = False
            self.values_list[6]["bold"] = True
            logging.getLogger("GUI").debug("Liquid nitrogen " + \
            "level in sample changer dewar is too low!")

        elif self.overflow_alarm not in [1, True, None]:
            self.values_list[6]["value"] = "Overflow alarm!"
            self.values_list[6]["in_range"] = False
            self.values_list[6]["bold"] = True
            logging.getLogger("GUI").debug(
                "Liquid nitrogen " + "overflow in sample changer dewar!"
            )
        else:
            self.values_list[6]["value"] = "Dewar level in range"
            self.values_list[6]["in_range"] = True
        
        logging.getLogger("HWR").debug("chan_sc_auto_refill %s" % self.chan_sc_auto_refill.get_value())
        if self.chan_sc_auto_refill.get_value() in [False, 0] and 'refill Off' not in self.values_list[6]["value"]:
            self.values_list[6]["value"] += "\nrefill Off"
        elif 'refill On' not in self.values_list[6]["value"]:
            self.values_list[6]["value"] += "\nrefill On"
        self.re_emit_values()

    def flux_changed(self, value, beam_info=None, transmission=None):
        """Sets flux value"""
        value = self.get_flux()
        self.values_list[3]["value"] = value
        msg_str = "%.2e ph/s" % value
        # msg_str += "\n@ %.1f transmission , %d x %d beam" % (\
        # transmission, beam_info['size_x'] * 1000, beam_info['size_y'] * 1000)
        self.values_list[3]["value_str"] = msg_str
        self.values_list[3]["in_range"] = value > 1e6
        self.re_emit_values()

    def get_flux(self):
        if HWR.beamline.flux is not None:
            return HWR.beamline.flux.get_value()
        else:
            try:
                f = self.flux_monitor.get_flux()
            except:
                f = self.flux_monitor_mockup.get_flux()
            return f
        
    def energy_changed(self, value, min_delta=0.005):
        #logging.getLogger().debug('energy_changed unfiltered')
        if hasattr(self, 'last_energy_value') and abs(value - self.last_energy_value) < min_delta:
            self.last_energy_value = value
            logging.getLogger().debug('energy_changed')
            self.flux_changed(-1)
            
    def re_emit_values(self):
        """Emits list of values"""
        self.emit("valuesChanged", self.values_list)

    def get_values(self):
        """Returns list of values"""
        val = dict(self.values_list)
        return val

    def temperature_changed(self, value):
        """"Update hutch temperature"""
        if type(value) == type(None):
            return
        self.values_list[2]["value"] = "%.1f C" % value
        self.values_list[2]["in_range"] = value < 25  # self.limits_dict['temp']
        self.re_emit_values()

    def get_temp_hum_values(self, sleep_time):
        """Updates temperatur and humidity values"""
        while True:
            temp = self.get_external_value(self.hutch_temp_addr)
            hum = self.get_external_value(self.hutch_hum_addr)
            if not None in (temp, hum):
                if abs(float(temp) - self.hutch_temp) > 0.1 or abs(
                    float(hum) != self.hutch_hum > 1
                ):
                    self.hutch_temp = temp
                    self.hutch_hum = hum
                    self.values_list[2]["value"] = "%.1f C, %.1f %%" % (temp, hum)
                    self.values_list[2]["in_range"] = temp < 25 and hum < 60
                    self.re_emit_values()
            time.sleep(sleep_time)

    def get_current(self):
        "Returns current" ""
        return self.values_list[0]["value"]

    def get_current_value(self):
        """Returns current"""
        return self.values_list[0]["value"]

    def get_message(self):
        """Returns synchrotron state text"""
        state_text = '%s' % str(self.state_text)
        return state_text

    def update_ramdisk_size(self, sleep_time=0.1):
        #while True:
        total, free, perc = self.get_ramdisk_size()
        if None in (total, free, perc):
            txt = " Unable to read ramdisk size!"
            self.values_list[-1]["in_range"] = False
        else:
            txt = " Total: %s\n Free:  %s (%s)" % (
                self.sizeof_fmt(total),
                self.sizeof_fmt(free),
                "{0:.0%}".format(perc),
            )
            self.values_list[-1]["in_range"] = free / 2 ** 30 > 10
        self.values_list[-1]["value"] = txt
        self.re_emit_values()
        #time.sleep(sleep_time)

    def get_ramdisk_size(self, data_dir="/nfs/data4"):
        if os.path.exists(data_dir):
            st = os.statvfs(data_dir)

            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            perc = st.f_bavail / float(st.f_blocks)
            return total, free, perc
        else:
            return None, None, None

    def sizeof_fmt(self, num):
        """Returns disk space formated in string"""

        try:
            for x in ["bytes", "KB", "MB", "GB"]:
                if num < 1024.0:
                    return "%3.1f%s" % (num, x)
                num /= 1024.0
            return "%3.1f%s" % (num, "TB")
        except Exception:
            return "???"
