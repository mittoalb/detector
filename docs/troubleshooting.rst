Troubleshooting
===============

Startup errors
--------------

.. list-table::
   :header-rows: 1
   :widths: 30 35 35

   * - Symptom
     - Cause
     - Fix
   * - ``IPC socket … in use by another IOC``
     - Another IOC with the same prefix is already running under
       your user.
     - ``ps -u $USER | grep run_ioc`` — either use it or kill it
       first.
   * - ``Cannot remove stale IPC socket … Operation not permitted``
     - Socket file left by a different user.
     - ``sudo rm /tmp/detector_ioc_*.sock``, then retry.
   * - ``spinError=-1005`` (Oryx, access denied)
     - Camera already opened by another process (ADSpinnaker C++
       IOC, another Python session).
     - Stop the other process first.
   * - GUI shows sensor size but no live frames
     - ``<prefix>image1:ArrayData`` PVA channel unreachable
       (firewall / different subnet).
     - Check ``<prefix>image1:ArrayData`` is reachable with
       ``pvget``.

Camera-specific issues
----------------------

See the notes at the bottom of :doc:`cameras/oryx`, :doc:`cameras/kinetix`,
and :doc:`cameras/orca` for the vendor-specific quirks (Spinnaker
exclusive-access, PVCAM device ACLs, DCAM ``RTLD_GLOBAL``, etc.).
