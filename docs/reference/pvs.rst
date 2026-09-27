EPICS PVs and tomoscan
======================

Served PVs
----------

Standard areaDetector PVs are served under ``<prefix>cam1:`` and
``<prefix>HDF1:``. Run:

.. code-block:: bash

   python run_ioc.py --camera <type> --list-pvs

to see all 65+ PVs.

tomoscan integration
--------------------

Configure tomoscan with:

.. code-block:: text

   CameraPVPrefix      = <prefix>:
   FilePluginPVPrefix  = <prefix>:HDF1:

Step-scan and fly-scan workflows are supported. The HDF5 plugin writes
frames to dataset ``/exchange/data`` with shape ``(N, height, width)``
uint16.

PVAccess streaming
------------------

Frames are also published as ``NTNDArray`` on
``<prefix>image1:ArrayData`` (default), throttled to 30 fps for display.
Use with ``pyqtstream`` or any PVA NTNDArray viewer:

.. code-block:: bash

   python pyqtstream.py --pv <prefix>image1:ArrayData
