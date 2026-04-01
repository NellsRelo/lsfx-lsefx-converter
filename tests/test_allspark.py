"""Unit tests for AllSpark registry error handling — no game data required."""

import pytest

from converter.allspark import AllSparkRegistry
from converter.errors import RegistryError


class TestRegistryErrors:
    def test_xcd_file_not_found(self):
        reg = AllSparkRegistry()
        with pytest.raises(FileNotFoundError, match="AllSpark XCD file not found"):
            reg.load_xcd("/nonexistent/ComponentDefinition.xcd")

    def test_xmd_file_not_found(self):
        reg = AllSparkRegistry()
        with pytest.raises(FileNotFoundError, match="AllSpark XMD file not found"):
            reg.load_xmd("/nonexistent/ModuleDefinition.xmd")

    def test_malformed_xcd(self, tmp_path):
        """A non-XML XCD file should raise RegistryError."""
        bad_xcd = tmp_path / "bad.xcd"
        bad_xcd.write_text("this is not xml {{{", encoding="utf-8")
        reg = AllSparkRegistry()
        with pytest.raises(RegistryError, match="Malformed XCD file"):
            reg.load_xcd(str(bad_xcd))

    def test_malformed_xmd(self, tmp_path):
        """A non-XML XMD file should raise RegistryError."""
        bad_xmd = tmp_path / "bad.xmd"
        bad_xmd.write_text("<<<not valid xml>>>", encoding="utf-8")
        reg = AllSparkRegistry()
        with pytest.raises(RegistryError, match="Malformed XMD file"):
            reg.load_xmd(str(bad_xmd))

    def test_empty_xcd(self, tmp_path):
        """An empty (but valid XML) XCD should load without error."""
        xcd = tmp_path / "empty.xcd"
        xcd.write_text("<components/>", encoding="utf-8")
        reg = AllSparkRegistry()
        reg.load_xcd(str(xcd))
        assert len(reg.components) == 0

    def test_empty_xmd(self, tmp_path):
        """An empty (but valid XML) XMD should load without error."""
        xmd = tmp_path / "empty.xmd"
        xmd.write_text("<modules/>", encoding="utf-8")
        reg = AllSparkRegistry()
        reg.load_xmd(str(xmd))
        assert len(reg.modules) == 0
