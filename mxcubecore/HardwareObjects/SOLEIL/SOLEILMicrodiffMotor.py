import logging
from gevent import spawn
from MicrodiffMotor import MicrodiffMotor
try:
    from goniometer import goniometer
except ModuleNotFoundError:
    from experimental_methods import goniometer
    
class SOLEILMicrodiffMotor(MicrodiffMotor):
    
    def __init__(self, name):
        MicrodiffMotor.__init__(self, name)
        self.goniometer = goniometer()
        
    def init(self):
        MicrodiffMotor.init(self)
        
    #def move(self, position, wait=True, timeout=None):
        #if abs(self.get_position() - position) >= self.motor_resolution:
            #if hasattr(self.goniometer, 'set_%s_position' % self.motor_name.lower()):
                #spawn(getattr(self.goniometer, 'set_%s_position' % self.motor_name.lower()), position)
            #else:
                #spawn(self.goniometer.set_position, {self.motor_name: position})


    def _set_value(self, position, wait=True, timeout=None):
        #print('set_position', self.actuator_name, self.motor_resolution)
        if abs(self.get_value() - position) >= self.motor_resolution:
            if self.actuator_name.lower() == 'kappa':
                print('setting kappa %.2f ' % position)
                spawn(getattr(self.goniometer, 'set_kappa_position'), position) #, 0)
            elif self.actuator_name.lower() == 'phi':
                print('setting phi %.2f ' % position)
                spawn(getattr(self.goniometer, 'set_phi_position'), position)
            elif self.actuator_name.lower() == 'phi':
                print('setting phi %.2f ' % position)
                spawn(getattr(self.goniometer, 'set_chi_position'), position)
            elif hasattr(self.goniometer, 'set_%s_position' % self.actuator_name.lower()):
                spawn(getattr(self.goniometer, 'set_%s_position' % self.actuator_name.lower()), position)
            else:
                self.position_attr.set_value(position)
                #spawn(self.goniometer.set_position, {self.actuator_name: position})
            
