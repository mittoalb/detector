"""Parser + writer tests for detectors.server.hdf5_layout."""

from __future__ import annotations

import tempfile
from pathlib import Path

import h5py
import numpy as np
import pytest

from detectors.server.hdf5_layout import (
    LayoutWriter,
    parse_layout_xml,
)


FIXTURE = str(Path(__file__).parent / "fixtures" / "TomoScanLayout.xml")


class TestParser:

    def test_real_xml_parses(self):
        parsed = parse_layout_xml(FIXTURE)
        assert parsed.routing_ndattr == "HDF5FrameLocation"
        assert parsed.catch_all_path == "/defaults"
        assert set(parsed.detector_dataset_paths) == {
            "/exchange/data",
            "/exchange/data_white",
            "/exchange/data_dark",
        }
        # Real XML has 173 named ndattribute datasets in the catch-all excludes
        assert len(parsed.consumed_ndattrs) > 100

    def test_top_level_groups(self):
        parsed = parse_layout_xml(FIXTURE)
        names = {c.name for c in parsed.root.children if c.kind == "group"}
        assert {"exchange", "measurement", "process", "defaults"} <= names

    def test_multiple_globals_raise(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("""<?xml version="1.0"?>
<hdf5_layout>
  <global name="detector_data_destination" ndattribute="A"/>
  <global name="detector_data_destination" ndattribute="B"/>
</hdf5_layout>""")
        with pytest.raises(ValueError, match="expected exactly one <global>"):
            parse_layout_xml(str(bad))

    def test_multiple_catch_all_groups_raise(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("""<?xml version="1.0"?>
<hdf5_layout>
  <global name="detector_data_destination" ndattribute="H"/>
  <group name="a" ndattr_default="true"/>
  <group name="b" ndattr_default="true">
    <dataset name="x" source="detector"/>
  </group>
</hdf5_layout>""")
        with pytest.raises(ValueError, match="multiple ndattr_default"):
            parse_layout_xml(str(bad))

    def test_no_detector_dataset_raises(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("""<?xml version="1.0"?>
<hdf5_layout>
  <global name="detector_data_destination" ndattribute="H"/>
  <group name="a"/>
</hdf5_layout>""")
        with pytest.raises(ValueError, match="no <dataset source='detector'>"):
            parse_layout_xml(str(bad))


class TestWriterEndToEnd:
    """Write a full file with the real XML + a controlled snapshot,
    then verify the resulting HDF5 structure matches expectations."""

    @pytest.fixture
    def outfile(self, tmp_path):
        return str(tmp_path / "out.h5")

    def _snap(self, location):
        # Values we care about verifying downstream — leave the rest to None
        return {
            "HDF5FrameLocation": location,
            "RingCurrent": 102.3,
            "Energy": 9.6,
            "SampleName": "TestSample",
            "UserLastName": "Mittone",
            "NumAngles": 750,
            "SourceEnergy": 9600.0,
        }

    def test_frames_split_by_frame_location(self, outfile):
        layout = parse_layout_xml(FIXTURE)
        rng = np.random.default_rng(0)
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, self._snap("/exchange/data_white"))
            for _ in range(3):
                w.write_frame(
                    f, rng.integers(0, 65536, (32, 40), dtype=np.uint16),
                    self._snap("/exchange/data_white"))
            for _ in range(5):
                w.write_frame(
                    f, rng.integers(0, 65536, (32, 40), dtype=np.uint16),
                    self._snap("/exchange/data"))
            for _ in range(2):
                w.write_frame(
                    f, rng.integers(0, 65536, (32, 40), dtype=np.uint16),
                    self._snap("/exchange/data_dark"))
            w.close(f, self._snap("/exchange/data"))

        with h5py.File(outfile, "r") as f:
            assert f["/exchange/data"].shape == (5, 32, 40)
            assert f["/exchange/data_white"].shape == (3, 32, 40)
            assert f["/exchange/data_dark"].shape == (2, 32, 40)

    def test_constant_datasets_written(self, outfile):
        layout = parse_layout_xml(FIXTURE)
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, self._snap("/exchange/data"))
            w.write_frame(f, np.zeros((4, 4), dtype=np.uint16),
                          self._snap("/exchange/data"))
            w.close(f, self._snap("/exchange/data"))

        with h5py.File(outfile, "r") as f:
            # One of the constant datasets: /measurement/instrument/name = "TXM"
            val = f["/measurement/instrument/name"][()]
            if isinstance(val, bytes):
                val = val.decode()
            assert val == "TXM"
            # Some source metadata is also a constant
            assert "beamline" in f["/measurement/instrument/source"]

    def test_ndattribute_datasets_written(self, outfile):
        layout = parse_layout_xml(FIXTURE)
        snap = self._snap("/exchange/data")
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, snap)
            w.write_frame(f, np.zeros((4, 4), dtype=np.uint16), snap)
            w.close(f, snap)

        with h5py.File(outfile, "r") as f:
            # RingCurrent → /measurement/instrument/source/current
            assert f["/measurement/instrument/source/current"][()] == 102.3
            # SampleName → /measurement/sample/name
            name = f["/measurement/sample/name"][()]
            assert (name.decode() if isinstance(name, bytes) else name) == "TestSample"

    def test_hdf5_attributes_on_datasets(self, outfile):
        layout = parse_layout_xml(FIXTURE)
        snap = self._snap("/exchange/data")
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, snap)
            w.write_frame(f, np.zeros((4, 4), dtype=np.uint16), snap)
            w.close(f, snap)

        with h5py.File(outfile, "r") as f:
            data_attrs = dict(f["/exchange/data"].attrs)
            assert data_attrs.get("description") == "ImageData"
            assert data_attrs.get("axes") == "theta:y:x"
            assert data_attrs.get("units") == "counts"

    def test_defaults_catch_all_contains_extras(self, outfile):
        layout = parse_layout_xml(FIXTURE)
        # Add attributes NOT consumed by any named dataset — they should
        # appear under /defaults/
        snap = self._snap("/exchange/data")
        snap["MyCustomAttr"] = 42.5
        snap["AnotherExtra"] = "hello"
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, snap)
            w.write_frame(f, np.zeros((4, 4), dtype=np.uint16), snap)
            w.close(f, snap)

        with h5py.File(outfile, "r") as f:
            defaults = list(f["/defaults"].keys())
            assert "MyCustomAttr" in defaults
            assert "AnotherExtra" in defaults
            # Consumed attrs must NOT show up in /defaults
            assert "RingCurrent" not in defaults
            assert "SampleName" not in defaults
            # Routing key + legacy datasets are excluded
            assert "HDF5FrameLocation" not in defaults

    def test_missing_routing_key_falls_back(self, outfile, caplog):
        layout = parse_layout_xml(FIXTURE)
        snap = dict(self._snap("/exchange/data"))
        snap["HDF5FrameLocation"] = ""    # empty routing key
        with h5py.File(outfile, "w", libver="latest", locking=False) as f:
            w = LayoutWriter(layout)
            w.open(f, snap)
            path = w.write_frame(
                f, np.zeros((4, 4), dtype=np.uint16), snap)
            w.close(f, snap)
            # Fallback path is /exchange/data
            assert path == "/exchange/data"
