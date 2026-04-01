"""Unit tests for LSF reader/writer — synthetic binary fixtures, no game data."""

import io
import struct
import uuid

from converter.errors import LsfParseError
from converter.lsf_reader import _read_lsf_guid, read_lsf
from converter.lsf_writer import _write_lsf_guid, write_lsf
from converter.lsx_model import (
    LSF_SIGNATURE,
    LsxNode,
    LsxNodeAttribute,
    LsxRegion,
    LsxResource,
)
import pytest


# ── GUID serialization roundtrip ────────────────────────────────────

class TestGuidRoundtrip:
    def test_known_guid(self):
        g = uuid.UUID("f7fc084b-d098-0d9a-8033-1cb61c3beb37")
        buf = io.BytesIO()
        _write_lsf_guid(buf, g)
        raw = buf.getvalue()
        assert len(raw) == 16
        result = _read_lsf_guid(raw)
        assert result == g

    def test_nil_guid(self):
        g = uuid.UUID(int=0)
        buf = io.BytesIO()
        _write_lsf_guid(buf, g)
        result = _read_lsf_guid(buf.getvalue())
        assert result == g

    def test_max_guid(self):
        g = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        buf = io.BytesIO()
        _write_lsf_guid(buf, g)
        result = _read_lsf_guid(buf.getvalue())
        assert result == g

    def test_random_guids(self):
        for _ in range(10):
            g = uuid.uuid4()
            buf = io.BytesIO()
            _write_lsf_guid(buf, g)
            assert _read_lsf_guid(buf.getvalue()) == g


# ── Write → Read roundtrip (synthetic LsxResource) ─────────────────

class TestWriteReadRoundtrip:
    def _make_resource(self) -> LsxResource:
        """Build a minimal LsxResource with a few typed attributes."""
        attrs = [
            LsxNodeAttribute(id="Name", attr_type="LSString", value="TestNode"),
            LsxNodeAttribute(id="Duration", attr_type="float", value="5.0"),
            LsxNodeAttribute(id="Count", attr_type="uint32", value="42"),
            LsxNodeAttribute(id="ID", attr_type="guid",
                             value="a1b2c3d4-e5f6-7890-abcd-ef1234567890"),
            LsxNodeAttribute(id="Enabled", attr_type="bool", value="True"),
        ]
        child = LsxNode(
            id="ChildNode",
            attributes=[
                LsxNodeAttribute(id="Value", attr_type="float", value="1.5"),
            ],
        )
        root = LsxNode(id="TestRoot", attributes=attrs, children=[child])
        region = LsxRegion(id="TestRegion", nodes=[root])
        return LsxResource(regions=[region])

    def test_roundtrip_preserves_structure(self):
        original = self._make_resource()
        buf = io.BytesIO()
        write_lsf(original, buf)
        buf.seek(0)
        restored = read_lsf(buf)

        assert len(restored.regions) == 1
        assert restored.regions[0].id == "TestRegion"

        # The writer wraps region.nodes under a virtual root with
        # the region's id; the reader preserves that wrapper.
        wrapper = restored.regions[0].nodes[0]
        assert wrapper.id == "TestRegion"

        root = wrapper.children[0]
        assert root.id == "TestRoot"
        assert len(root.attributes) == 5
        assert len(root.children) == 1
        assert root.children[0].id == "ChildNode"

    def test_roundtrip_preserves_values(self):
        original = self._make_resource()
        buf = io.BytesIO()
        write_lsf(original, buf)
        buf.seek(0)
        restored = read_lsf(buf)

        root = restored.regions[0].nodes[0].children[0]
        assert root.attr_value("Name") == "TestNode"
        assert root.attr_value("Count") == "42"
        assert root.attr_value("Enabled") == "True"
        assert root.attr_value("ID") == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

        # Float might have minor formatting difference, compare numerically
        dur = float(root.attr_value("Duration"))
        assert abs(dur - 5.0) < 1e-6

        child = root.children[0]
        cval = float(child.attr_value("Value"))
        assert abs(cval - 1.5) < 1e-6

    def test_empty_resource(self):
        empty = LsxResource(regions=[])
        buf = io.BytesIO()
        write_lsf(empty, buf)
        buf.seek(0)
        restored = read_lsf(buf)
        assert len(restored.regions) == 0

    def test_multiple_regions(self):
        r1 = LsxRegion(id="Region1", nodes=[
            LsxNode(id="Node1", attributes=[
                LsxNodeAttribute(id="A", attr_type="uint32", value="1"),
            ]),
        ])
        r2 = LsxRegion(id="Region2", nodes=[
            LsxNode(id="Node2", attributes=[
                LsxNodeAttribute(id="B", attr_type="uint32", value="2"),
            ]),
        ])
        resource = LsxResource(regions=[r1, r2])
        buf = io.BytesIO()
        write_lsf(resource, buf)
        buf.seek(0)
        restored = read_lsf(buf)
        assert len(restored.regions) == 2
        assert {r.id for r in restored.regions} == {"Region1", "Region2"}

    def test_string_types(self):
        """Test various string type attributes roundtrip."""
        node = LsxNode(id="Strings", attributes=[
            LsxNodeAttribute(id="FS", attr_type="FixedString", value="hello"),
            LsxNodeAttribute(id="LS", attr_type="LSString", value="world"),
            LsxNodeAttribute(id="Path", attr_type="path", value="assets/test.dds"),
        ])
        resource = LsxResource(regions=[LsxRegion(id="R", nodes=[node])])
        buf = io.BytesIO()
        write_lsf(resource, buf)
        buf.seek(0)
        restored = read_lsf(buf)
        root = restored.regions[0].nodes[0].children[0]
        assert root.attr_value("FS") == "hello"
        assert root.attr_value("LS") == "world"
        assert root.attr_value("Path") == "assets/test.dds"


# ── Binary construction helpers ─────────────────────────────────────

def _lsf_header(
    sig: int = LSF_SIGNATURE,
    version: int = 6,
    engine_version: int = 0,
) -> bytes:
    """Build a minimal LSF header (signature + version + engine_version)."""
    buf = struct.pack("<II", sig, version)
    if version >= 5:
        buf += struct.pack("<q", engine_version)
    else:
        buf += struct.pack("<i", engine_version)
    return buf


def _metadata_v6_bytes(
    *,
    strings_unc: int = 0, strings_disk: int = 0,
    keys_unc: int = 0, keys_disk: int = 0,
    nodes_unc: int = 0, nodes_disk: int = 0,
    attrs_unc: int = 0, attrs_disk: int = 0,
    values_unc: int = 0, values_disk: int = 0,
    compression: int = 0,
    meta_format: int = 1,
) -> bytes:
    """Version 6 metadata: 10 u32s + u8 + u8 + u16 + u32 = 48 bytes."""
    buf = io.BytesIO()
    for v in [strings_unc, strings_disk, keys_unc, keys_disk,
              nodes_unc, nodes_disk, attrs_unc, attrs_disk,
              values_unc, values_disk]:
        buf.write(struct.pack("<I", v))
    buf.write(struct.pack("<B", compression))
    buf.write(struct.pack("<B", 0))
    buf.write(struct.pack("<H", 0))
    buf.write(struct.pack("<I", meta_format))
    return buf.getvalue()


# ── Bad / corrupt binary input ──────────────────────────────────────

class TestBadSignature:
    def test_wrong_signature(self):
        data = _lsf_header(sig=0xDEADBEEF)
        with pytest.raises(LsfParseError, match="Bad LSF signature"):
            read_lsf(io.BytesIO(data))

    def test_zero_signature(self):
        data = _lsf_header(sig=0x00000000)
        with pytest.raises(LsfParseError, match="Bad LSF signature"):
            read_lsf(io.BytesIO(data))


class TestBadVersion:
    def test_version_too_low(self):
        data = _lsf_header(version=1) + _metadata_v6_bytes()
        with pytest.raises(LsfParseError, match="Unsupported LSF version 1"):
            read_lsf(io.BytesIO(data))

    def test_version_too_high(self):
        data = _lsf_header(version=99) + _metadata_v6_bytes()
        with pytest.raises(LsfParseError, match="Unsupported LSF version 99"):
            read_lsf(io.BytesIO(data))


class TestTruncatedInput:
    def test_empty_stream(self):
        with pytest.raises(LsfParseError, match="Unexpected EOF"):
            read_lsf(io.BytesIO(b""))

    def test_truncated_after_signature(self):
        data = struct.pack("<I", LSF_SIGNATURE)  # just 4 bytes, no version
        with pytest.raises(LsfParseError, match="Unexpected EOF"):
            read_lsf(io.BytesIO(data))

    def test_truncated_metadata(self):
        hdr = _lsf_header()
        data = hdr + b"\x00" * 10  # partial metadata
        with pytest.raises(LsfParseError, match="Unexpected EOF"):
            read_lsf(io.BytesIO(data))


class TestSectionOversize:
    def test_section_too_large(self):
        hdr = _lsf_header()
        meta = _metadata_v6_bytes(strings_unc=300_000_000)  # > 256 MB
        data = hdr + meta
        with pytest.raises(LsfParseError, match="LSF section too large"):
            read_lsf(io.BytesIO(data))

    def test_compressed_section_too_large(self):
        hdr = _lsf_header()
        meta = _metadata_v6_bytes(strings_unc=100, strings_disk=300_000_000)
        data = hdr + meta
        with pytest.raises(LsfParseError, match="LSF compressed section too large"):
            read_lsf(io.BytesIO(data))


class TestFileNotFound:
    def test_read_nonexistent_file(self):
        with pytest.raises(FileNotFoundError, match="LSF file not found"):
            read_lsf("/nonexistent/path/fake.lsfx")


# ── Writer type / encoding errors ───────────────────────────────────

class TestWriterUnknownType:
    def test_unknown_type_id_defaults_to_none(self):
        """Writer maps unrecognized attr_type to type_id=0 (none), writes without raising."""
        attr = LsxNodeAttribute(id="Test", attr_type="CompletelyFakeType", value="42")
        node = LsxNode(id="TestNode", attributes=[attr])
        region = LsxRegion(id="TestRegion", nodes=[node])
        res = LsxResource(regions=[region])
        buf = io.BytesIO()
        write_lsf(res, buf)  # should not raise
        assert buf.tell() > 0

    def test_bad_value_for_type(self):
        """A non-numeric string for a uint32 attribute should raise ValueError."""
        attr = LsxNodeAttribute(id="BadVal", attr_type="uint32", value="not_a_number")
        node = LsxNode(id="TestNode", attributes=[attr])
        region = LsxRegion(id="TestRegion", nodes=[node])
        res = LsxResource(regions=[region])
        with pytest.raises(ValueError, match="Cannot encode attribute"):
            buf = io.BytesIO()
            write_lsf(res, buf)


# ── GUID edge cases ─────────────────────────────────────────────────

class TestGuidEdgeCases:
    def test_invalid_guid_in_reader(self):
        """Reader rejects GUID with wrong byte count."""
        with pytest.raises(LsfParseError, match="Invalid GUID byte length"):
            _read_lsf_guid(b"\x00" * 15)

    def test_invalid_guid_too_long(self):
        with pytest.raises(LsfParseError, match="Invalid GUID byte length"):
            _read_lsf_guid(b"\x00" * 17)

    def test_empty_guid_bytes(self):
        with pytest.raises(LsfParseError, match="Invalid GUID byte length"):
            _read_lsf_guid(b"")

    def test_writer_invalid_guid_writes_nil(self):
        """Writer should warn and write a nil GUID for invalid GUID strings."""
        attr = LsxNodeAttribute(id="MapKey", attr_type="guid", value="not-a-guid")
        node = LsxNode(id="TestNode", attributes=[attr])
        region = LsxRegion(id="TestRegion", nodes=[node])
        res = LsxResource(regions=[region])
        buf = io.BytesIO()
        write_lsf(res, buf)  # succeeds with warning to stderr, does not raise
        assert buf.tell() > 0
