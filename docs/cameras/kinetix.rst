Teledyne Photometrics Kinetix (PVCAM)
=====================================

Backend module: :mod:`detectors.cameras.teledyne.kinetix`.
Camera type identifier: ``teledyne.kinetix``.

Direct ``ctypes`` binding to ``libpvcam.so.2``. No ``pyvcam``
dependency.

Library resolution
------------------

The library is picked up from ``LD_LIBRARY_PATH`` first, then falls back
to the vendor path shipped with ADKinetix (``/opt/pvcam/library/x86_64/libpvcam.so.2``
on typical installs).

Exclusive access
----------------

PVCAM claims the camera exclusively. Stop any running ADKinetix C++ IOC
before starting this Python one.

First-open sequence
-------------------

On the first open, the backend:

* calls ``pl_pp_reset``,
* forces ``PARAM_EXP_RES`` to microseconds (Kinetix defaults to seconds
  — 10 ms exposure would round to 0),
* selects a valid speed-table entry,
* runs one prime setup+start+stop cycle.

Without any of these, ``pl_exp_start_cont`` returns silently with
``PL_ERR_CONFIGURATION_INVALID`` or ``PL_ERR_NONE``.

Acquisition loop
----------------

Uses the standard PVCAM EOF callback registered on the main thread,
with a ``threading.Event`` handoff so ``acquire_frame()`` releases the
GIL while waiting for the next frame — same pattern DCAM uses via
``dcamwait_start``.

Device permissions
------------------

``/dev/pvcamPCIE_0`` typically has ``group: users``. Users not in that
group need an explicit ACL entry:

.. code-block:: bash

   setfacl -m u:<user>:rw /dev/pvcamPCIE_0
