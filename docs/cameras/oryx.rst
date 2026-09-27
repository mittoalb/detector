Teledyne FLIR Oryx (Spinnaker)
==============================

Backend module: :mod:`detectors.cameras.teledyne.oryx`.
Camera type identifier: ``teledyne.oryx``.

Direct ``ctypes`` binding to ``libSpinnaker_C.so.2``. No ``PySpin``
dependency (Spinnaker Python wheels lag behind Python versions; ctypes
works on 3.13).

Library resolution
------------------

The library is picked up from ``LD_LIBRARY_PATH`` first, then falls back
to the vendor path shipped with ADSpinnaker at
``/APSshare/epics/synApps_6_2_1/support/areaDetector-R3-12-1/ADSpinnaker/spinnakerSupport/os/linux-x86_64/``.

The loader pre-loads transitive C++ dependencies (``libLog_*``,
``libMathParser_*``, ``libGenApi_*``, ``libSpinnaker.so.2``) with
``RTLD_GLOBAL`` so no shell-side ``LD_LIBRARY_PATH`` setup is required.

Exclusive access
----------------

Spinnaker claims the camera exclusively per process. Stop any running
ADSpinnaker C++ IOC (e.g. ``32idbSP1``) before starting this Python
one, and vice versa.

Selecting a specific camera
---------------------------

.. code-block:: bash

   python run_ioc.py --camera teledyne.oryx --serial 23605513 --prefix ORYX:

Serial matches the ADSpinnaker ``CAMERA_ID`` convention.

Parameter surface
-----------------

The backend exposes the **full GenICam node map** through
``list_params()`` (2000+ nodes on the Oryx). The GUI auto-groups
non-standard names by prefix (Analog/Gain, Trigger, Chunk Data,
Transport Layer, Device Info, …) so it stays browsable.

ROI-affecting nodes (``Width``, ``Height``, ``OffsetX/Y``,
``Binning*``, ``PixelFormat``) are locked during acquisition by the
SDK. ``set_param`` on those pauses acquisition, applies the write, and
restarts — writes go through mid-run without the caller having to
know.

Pixel formats
-------------

Only unpacked ``Mono8`` / ``Mono16`` are supported for acquisition.
``Mono12Packed`` raises a clear error at ``start_acquisition`` — set
``PixelFormat=Mono16`` from the GUI or via CA before starting.

Reported sensor size
--------------------

``SensorWidth`` / ``SensorHeight`` (true sensor size) are preferred over
``WidthMax`` / ``HeightMax`` when populating :class:`CameraInfo` —
``WidthMax`` is the max ROI *in the current binning mode* and
misreports when binning > 1.
