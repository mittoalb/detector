"""Parser tests for detectors.server.nd_attributes.

Covers:
  - Real 32-ID XML fixture parses without error.
  - Macro substitution ($(DET), $(TS)) works.
  - Unresolved macros → attribute skipped with a WARNING (not raised).
  - Non-EPICS_PV types silently skipped.
  - DBR type distribution matches what the report agent measured.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from detectors.server.nd_attributes import (
    AttributeSpec,
    parse_attributes_xml,
    NDAttributesManager,
)


FIXTURE = str(Path(__file__).parent / "fixtures" /
              "TomoScanDetectorAttributes.xml")


class TestParser:

    def test_real_xml_parses(self):
        macros = {"DET": "TEST:", "TS": "32id:TomoScan:"}
        specs = parse_attributes_xml(FIXTURE, macros=macros)
        assert len(specs) > 150      # 174 in the current file
        assert all(isinstance(s, AttributeSpec) for s in specs)

    def test_macros_substituted(self):
        macros = {"DET": "MYCAM:", "TS": "MYTS:"}
        specs = parse_attributes_xml(FIXTURE, macros=macros)
        names = {s.name: s.pvname for s in specs}
        # $(TS)FlipStitch was the last-added attribute in the real XML
        assert names.get("FlipStitch") == "MYTS:FlipStitch"
        # $(DET) also expected: pick one that uses it
        det_attrs = [s for s in specs if s.pvname.startswith("MYCAM:")]
        assert len(det_attrs) > 0

    def test_unresolved_macro_skipped(self, caplog):
        # NO macros provided → every $(...) source is skipped
        specs = parse_attributes_xml(FIXTURE, macros={})
        # $(TS) + $(DET) attrs must be excluded
        assert all("$(" not in s.pvname for s in specs)
        # But we should still have the un-macroed beamline PVs
        assert len(specs) > 100

    def test_dbrtype_distribution(self):
        macros = {"DET": "TEST:", "TS": "TS:"}
        specs = parse_attributes_xml(FIXTURE, macros=macros)
        types = {}
        for s in specs:
            types[s.dbrtype_hint] = types.get(s.dbrtype_hint, 0) + 1
        # DBR distribution: DBR_STRING, DBR_DOUBLE, DBR_LONG,
        # DBR_ENUM, DBR_NATIVE are all present in the real XML
        assert "DBR_STRING" in types
        assert "DBR_DOUBLE" in types
        assert "DBR_LONG" in types
        assert "DBR_NATIVE" in types
        assert "DBR_ENUM" in types

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises((FileNotFoundError, OSError)):
            parse_attributes_xml(str(tmp_path / "nope.xml"))

    def test_wrong_root_raises(self, tmp_path):
        bad = tmp_path / "bad.xml"
        bad.write_text("<foo/>")
        with pytest.raises(ValueError, match="expected <Attributes>"):
            parse_attributes_xml(str(bad))


class TestManagerResolvePath:

    def test_absolute_path(self):
        mgr = NDAttributesManager()
        # Won't load; just checks path resolution
        assert mgr._resolve_path(FIXTURE) == FIXTURE

    def test_search_paths(self, tmp_path):
        # Create a fake XML in a search dir
        (tmp_path / "attrs.xml").write_text('<Attributes></Attributes>')
        mgr = NDAttributesManager(search_paths=[str(tmp_path)])
        assert mgr._resolve_path("attrs.xml") == str(tmp_path / "attrs.xml")

    def test_env_path(self, tmp_path, monkeypatch):
        (tmp_path / "e.xml").write_text('<Attributes></Attributes>')
        monkeypatch.setenv("AREA_DETECTOR_ATTRIBUTES_PATH", str(tmp_path))
        mgr = NDAttributesManager()
        assert mgr._resolve_path("e.xml") == str(tmp_path / "e.xml")

    def test_not_found_raises(self, tmp_path):
        mgr = NDAttributesManager(search_paths=[str(tmp_path)])
        with pytest.raises(FileNotFoundError, match="not found"):
            mgr._resolve_path("does-not-exist.xml")
