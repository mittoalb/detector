Architecture
============

Overview
--------

::

                          run_ioc.py
                              │
                              ▼
        ┌──────── DetectorIOC (PVGroup) ────────┐
        │  cam1: PVs   HDF1: PVs   image1: PVA  │
        └──────────────────┬────────────────────┘
                           │ BaseCamera interface
                           ▼
       ┌───────────────────────────────────────┐
       │   Camera Registry (auto / explicit)   │
       └─────┬─────────┬─────────┬─────────────┘
             ▼         ▼         ▼
        Hamamatsu Teledyne   Simulator
         DCAM   Spinnaker/PVCAM
       (ORCA Fire) (Oryx/Kinetix)

The ``BaseCamera`` abstract class (:mod:`detectors.core.base`) defines a
uniform interface — every backend implements ``open``, ``close``,
``get_param``, ``set_param``, ``acquire_frame``, ``start_acquisition``,
``stop_acquisition``, and a few more.

Backends self-register via the ``@register`` decorator
(:mod:`detectors.core.registry`). Adding a new camera is a single file
in ``detectors/cameras/<vendor>/``.

GUI process model
-----------------

With ``--gui``, ``run_ioc.py`` launches the Qt GUI as a **separate
process** that talks to the driver process over three channels:

* **Channel Access** — the standard ``<prefix>cam1:`` / ``<prefix>HDF1:``
  PVs, for compatibility with tomoscan and other EPICS clients.
* **pvAccess NTNDArray** — live frames on ``<prefix>image1:ArrayData``.
* **Local UNIX-socket IPC** — a JSON-RPC channel at
  ``/tmp/detector_ioc_<prefix>.sock`` carrying the camera's *full*
  ``BaseCamera`` surface (every GenICam node on the Oryx, every DCAM
  property on the ORCA, every PVCAM param on the Kinetix). Lets the
  GUI enumerate and edit features without needing a matching EPICS PV
  for each one. Falls back to the CA gateway transparently if the
  socket isn't reachable.

Why the process split: some vendor drivers (notably Photometrics PVCAM
used by the Kinetix backend) install signal handlers that Qt's
``QApplication`` clobbers when they share a process. In-process Qt then
silently starves the camera's DMA loop. Running Qt in its own process
side-steps this uniformly for every backend — no per-camera branching
in the launcher, no threading hacks.

Directory layout
----------------

::

   detectors/
   ├── core/
   │   ├── base.py              # BaseCamera ABC + CameraInfo
   │   ├── registry.py          # Camera type registry / factory
   │   └── log_util.py          # ANSI color logging
   ├── cameras/
   │   ├── hamamatsu/
   │   │   ├── orca_fire_dcam.py     # ORCA Fire via DCAM
   │   │   └── orca_fire_gentl.py    # ORCA Fire via Harvesters/GenTL
   │   ├── teledyne/
   │   │   ├── oryx.py               # Oryx via Spinnaker
   │   │   └── kinetix.py            # Kinetix via PVCAM
   │   └── simulator.py              # Built-in simulator
   ├── server/
   │   ├── ioc.py               # DetectorIOC (top-level)
   │   ├── cam_plugin.py        # cam1: PVs (+ RO poller)
   │   ├── hdf5_plugin.py       # HDF1: PVs
   │   ├── tiff_plugin.py       # TIF1: PVs
   │   ├── ipc.py               # UNIX-socket JSON-RPC to the GUI
   │   └── ntnda_server.py      # PVAccess NTNDArray
   └── gui/
       ├── qt_gui.py            # Camera-agnostic Qt GUI
       ├── ca_proxy.py          # CA/PVA-backed IocProxy
       └── ipc_client.py        # UNIX-socket client (full-fidelity access)

   run_ioc.py                   # Main entry point
   check_environment.py         # Environment / hardware checker
   firebird-driver-rhel9-patch/ # Patched Active Silicon driver source
