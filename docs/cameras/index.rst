Cameras
=======

Currently supported cameras:

.. list-table::
   :header-rows: 1
   :widths: 30 40 30

   * - Backend
     - Camera
     - SDK
   * - ``hamamatsu.orca_fire_dcam``
     - Hamamatsu ORCA Fire C16240-20UP
     - DCAM-API + Active Silicon FireBird
   * - ``hamamatsu.orca_fire_gentl``
     - Hamamatsu ORCA Fire C16240-20UP
     - Harvesters / GenTL + Euresys Coaxlink
   * - ``teledyne.oryx``
     - Teledyne FLIR Oryx (10GigE / CXP)
     - Spinnaker C API (direct ctypes — no PySpin)
   * - ``teledyne.kinetix``
     - Teledyne Photometrics Kinetix
     - PVCAM (direct ctypes — no PyVCAM)
   * - ``simulator``
     - Synthetic frames
     - none — built in

.. toctree::
   :maxdepth: 1

   oryx
   kinetix
   orca
