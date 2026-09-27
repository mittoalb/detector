Installation
============

Base Python environment
-----------------------

.. code-block:: bash

   conda create -n detector python=3.11
   conda activate detector
   pip install numpy PyQt5 caproto h5py pvapy
   pip install imageio      # optional, for TIFF export
   pip install -e .         # editable install from the repo root

Per-camera SDKs
---------------

Camera SDKs are installed at **system level** by each vendor's installer,
not with ``pip``. The Python backends bind directly to the vendor
shared libraries via ``ctypes`` — no vendor Python wrappers required.

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Backend
     - Install
   * - ``hamamatsu.orca_fire_dcam``
     - DCAM-API Lite for Linux + FireBird kernel driver.
       See ``firebird-driver-rhel9-patch/README.md``.
   * - ``hamamatsu.orca_fire_gentl``
     - ``pip install harvesters`` + Euresys eGrabber.
   * - ``teledyne.oryx``
     - Spinnaker SDK. Provides ``libSpinnaker_C.so.2``. No Python bindings.
   * - ``teledyne.kinetix``
     - PVCAM SDK. Provides ``libpvcam.so.2``. No Python bindings.

The loader for each backend probes ``LD_LIBRARY_PATH`` first, then falls
back to well-known vendor install paths (``/usr/local/hamamatsu_dcam/``,
``/opt/pvcam/``, the ADSpinnaker vendor path). No shell-side
``LD_LIBRARY_PATH`` setup is required.

Environment check
-----------------

.. code-block:: bash

   python check_environment.py

Prints installed SDK versions, kernel drivers found, device permissions,
and which backends are runnable.
