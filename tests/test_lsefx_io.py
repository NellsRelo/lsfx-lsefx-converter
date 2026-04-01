"""Unit tests for LSEFX I/O error handling — no game data required."""

import pytest

from converter.effect_model import NIL_UUID
from converter.lsefx_io import read_lsefx


class TestLsefxIoErrors:
    def test_lsefx_file_not_found(self):
        with pytest.raises(FileNotFoundError, match="LSEFX file not found"):
            read_lsefx("/nonexistent/fake.lsefx")

    def test_lsefx_malformed_xml(self, tmp_path):
        bad = tmp_path / "bad.lsefx"
        bad.write_text("this is not XML at all", encoding="utf-8")
        with pytest.raises(ValueError, match="Malformed .lsefx XML"):
            read_lsefx(str(bad))

    def test_lsefx_wrong_root_element(self, tmp_path):
        bad = tmp_path / "wrong_root.lsefx"
        bad.write_text("<noteffect/>", encoding="utf-8")
        with pytest.raises(ValueError, match="Expected <effect> root"):
            read_lsefx(str(bad))

    def test_lsefx_empty_effect(self, tmp_path):
        """A minimal valid .lsefx should parse without error."""
        f = tmp_path / "minimal.lsefx"
        f.write_text(
            '<effect version="0.0" effectversion="1.0.0"'
            ' id="00000000-0000-0000-0000-000000000000"/>',
            encoding="utf-8",
        )
        result = read_lsefx(str(f))
        assert result.id == NIL_UUID
