Standard parameter names
========================

Backends **should** use these names where applicable so the GUI/IOC are
uniform across cameras. Backends raise :class:`KeyError` for
unsupported parameters; the GUI hides them. Backends may *also*
expose vendor-specific names (e.g. every GenICam node on the Oryx) —
the GUI groups those into auto-generated tabs alongside the standard
names.

.. list-table::
   :header-rows: 1
   :widths: 30 15 55

   * - Name
     - Type
     - Description
   * - ``ExposureTime``
     - float
     - Exposure in seconds.
   * - ``AcquisitionFrameRate``
     - float
     - Current frame rate (often read-only).
   * - ``Width`` / ``Height``
     - int
     - Image dimensions.
   * - ``OffsetX`` / ``OffsetY``
     - int
     - ROI offset.
   * - ``BinningHorizontal`` / ``BinningVertical``
     - int
     - Binning factor.
   * - ``PixelFormat``
     - str
     - ``Mono16``, ``Mono12``, …
   * - ``TriggerMode``
     - str
     - ``Off``, ``On``.
   * - ``TriggerSource``
     - str
     - ``Internal``, ``External``, ``Software``.
   * - ``TriggerActive``
     - str
     - ``Edge``, ``Level``.
   * - ``TriggerPolarity``
     - str
     - ``Positive``, ``Negative``.
   * - ``SensorMode``
     - str
     - ``Area``, ``Lightsheet``.
   * - ``ReadoutSpeed``
     - str
     - ``Fastest``, ``Slowest``.
   * - ``ShutterMode``
     - str
     - ``Rolling``, ``Global``.
   * - ``SensorTemperature``
     - float
     - Celsius (read-only).
   * - ``SensorCooler``
     - str
     - ``Off``, ``On``, ``Max``.
   * - ``SensorCoolerStatus``
     - str
     - ``Ready``, ``Busy``, ``Off``, ``Error``.
   * - ``SensorTemperatureTarget``
     - float
     - Cooler setpoint.
