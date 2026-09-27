Quickstart
==========

Install
-------

.. code-block:: bash

   conda create -n detector python=3.11
   conda activate detector
   pip install numpy PyQt5 caproto h5py pvapy
   pip install -e .              # from the repo root

Camera-specific SDKs must be installed at system level — see
:doc:`installation`.

Run
---

.. code-block:: bash

   # List available backends
   python run_ioc.py --list-cameras

   # Auto-detect a camera
   python run_ioc.py --prefix MYDET:

   # Specify backend explicitly
   python run_ioc.py --camera teledyne.oryx  --prefix ORYX:
   python run_ioc.py --camera teledyne.kinetix --prefix KTX:
   python run_ioc.py --camera hamamatsu.orca_fire_dcam --prefix ORCA:
   python run_ioc.py --camera simulator --prefix SIM:

   # With the Qt GUI (as a subprocess)
   python run_ioc.py --camera simulator --gui

See :doc:`running` for every flag and the reopen / stop workflow.

Verify
------

.. code-block:: bash

   caget MYDET:cam1:MaxSizeX_RBV
   pvget MYDET:image1:ArrayData     # live PVA stream (NTNDArray)
