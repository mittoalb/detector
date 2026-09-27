Hamamatsu ORCA Fire (DCAM / GenTL)
==================================

Two backends drive the ORCA Fire:

* :mod:`detectors.cameras.hamamatsu.orca_fire_dcam` — camera type
  ``hamamatsu.orca_fire_dcam``. Vendor DCAM-API + Active Silicon
  FireBird grabber.
* :mod:`detectors.cameras.hamamatsu.orca_fire_gentl` — camera type
  ``hamamatsu.orca_fire_gentl``. Harvesters / GenTL over Euresys
  eGrabber.

DCAM library and grabber driver
-------------------------------

The Hamamatsu DCAM backend requires:

* **DCAM-API Lite for Linux** installed at ``/usr/local/hamamatsu_dcam/``
  (vendor installer).
* A **supported frame grabber driver** — for FireBird on RHEL 9 see
  ``firebird-driver-rhel9-patch/README.md`` for the bind-mount
  workaround when ``/usr/local`` is read-only.

Critical: the DCAM library must be loaded with ``ctypes.RTLD_GLOBAL``
(the backend handles this) — otherwise the FireBird module can't
resolve symbols and DCAM returns ``NOCAMERA``.

GenTL backend
-------------

Uses ``pip install harvesters`` + Euresys eGrabber. Camera type
``hamamatsu.orca_fire_gentl``. Same ``BaseCamera`` surface; different
transport layer.
