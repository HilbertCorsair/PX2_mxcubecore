# Export SOLEILSession so that "SOLEIL.SOLEILSession" resolves when
# the package is loaded by HardwareRepository (getattr(module, class_name)).
from .SOLEILSession import SOLEILSession

__all__ = ["SOLEILSession"]
