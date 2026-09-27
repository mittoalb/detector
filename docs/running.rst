Running
=======

Launch
------

.. code-block:: bash

   # List backends
   python run_ioc.py --list-cameras

   # Auto-detect a camera
   python run_ioc.py --prefix MYDET:

   # Explicit backend
   python run_ioc.py --camera teledyne.oryx --prefix ORYX:
   python run_ioc.py --camera simulator     --prefix SIM:

   # Pick a specific Oryx by serial (matches ADSpinnaker CAMERA_ID)
   python run_ioc.py --camera teledyne.oryx --serial 23605513 --prefix ORYX:

   # With Qt GUI (subprocess; connects via CA/PVA + local IPC)
   python run_ioc.py --camera simulator --gui

   # Show every served PV
   python run_ioc.py --camera simulator --list-pvs

   # Custom PVA stream PV
   python run_ioc.py --camera simulator --pva-pv MYDET:image:NTNDArray

Reopen the GUI without restarting the IOC
-----------------------------------------

If you close the Qt window but the IOC is still running, reattach a
fresh GUI without touching the camera:

.. code-block:: bash

   python run_ioc.py --gui-only --prefix ORYX:

Camera state, acquisition, and any in-flight HDF5 capture are preserved.

Stop a running IOC
------------------

``Ctrl-C`` in the terminal is the intended shutdown — it cleans up the
IPC socket, releases the camera SDK, and terminates the GUI subprocess.

If the terminal is gone or the process is stuck:

.. code-block:: bash

   # Find your IOC(s)
   ps -u $USER -o pid,stat,cmd | grep run_ioc | grep -v grep

   # Graceful termination (recommended — lets the camera close properly)
   pkill -TERM -u $USER -f "run_ioc.py.*--prefix ORYX:"

   # Only if TERM doesn't work after ~5 seconds
   pkill -KILL -u $USER -f "run_ioc.py.*--prefix ORYX:"

``-KILL`` may leave the camera in a bad state — you may need a
power-cycle.

Logging colors
--------------

The console uses ANSI-256 colors by log level. Standard env vars:

* ``NO_COLOR=1`` — disable regardless of TTY (`no-color.org
  <https://no-color.org>`_)
* ``FORCE_COLOR=1`` or ``CLICOLOR_FORCE=1`` — enable even when piped
* default: auto-detect via ``sys.stderr.isatty()``
