Adding a new camera
===================

Every backend is a single file under ``detectors/cameras/<vendor>/`` that
subclasses :class:`detectors.core.base.BaseCamera` and registers itself
with the ``@register`` decorator.

Skeleton
--------

.. code-block:: python

   from detectors.core.base import BaseCamera, CameraInfo
   from detectors.core.registry import register

   @register
   class MyNewCamera(BaseCamera):
       camera_type = "vendor.model"
       display_name = "Vendor Model XYZ"

       def open(self):
           # Open camera via vendor SDK
           self._info = CameraInfo(vendor="Vendor", model="XYZ", ...)

       def close(self):
           ...

       def get_param(self, name):
           ...

       def set_param(self, name, value):
           ...

       def list_params(self):
           return ["ExposureTime", "Width", "Height", ...]

       def start_acquisition(self):
           ...

       def stop_acquisition(self):
           ...

       def acquire_frame(self, timeout_ms=5000):
           # Return numpy array (height, width)
           ...

Wire-up
-------

Add the module path to :func:`detectors.core.registry.load_all`. That's
the only file outside your new backend module that needs to change —
the IOC, GUI, and tomoscan integration work without further edits.

Standard parameter names
------------------------

Use the names in :doc:`reference/parameters` where they apply, so the
GUI stays uniform across cameras. Vendor-specific names are also fine —
the GUI groups them into auto-generated tabs.
