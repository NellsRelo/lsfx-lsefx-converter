"""LSF binary writer — serializes an LsxResource tree to LSF binary format.

Writes LSF version 6 with LZ4 frame compression and KeysAndAdjacency
metadata format, matching typical BG3 .lsfx files.
"""

import io
import struct
import sys
import uuid
from collections import defaultdict
from typing import BinaryIO

import lz4.block
import lz4.frame

from . import _output
from .lsx_model import (
    LSF_SIGNATURE,
    NAME_TO_TYPE_ID,
    LsxNode,
    LsxNodeAttribute,
    LsxResource,
    LsxTranslatedFsArgument,
)

# ── Constants ───────────────────────────────────────────────────────

WRITE_VERSION = 6
ENGINE_VERSION: int = 0x0000_0004_0000_0027  # 4.0.0.39 — typical BG3
COMPRESSION_LZ4 = 2
METADATA_FORMAT_KEYS_AND_ADJACENCY = 1
STRING_HASH_MAP_SIZE = 0x200  # 512 buckets

# ── Public API ──────────────────────────────────────────────────────


def write_lsf(resource: LsxResource, dest: str | BinaryIO) -> None:
    """Serialize *resource* to LSF binary.

    *dest* may be a file path or a writable binary stream.
    """
    if isinstance(dest, str):
        with open(dest, "wb") as fh:
            _write(resource, fh)
    else:
        _write(resource, dest)


# ── Writer core ─────────────────────────────────────────────────────

def _write(resource: LsxResource, out: BinaryIO) -> None:
    if not resource.regions:
        _output.warnings.warn("Writing empty LsxResource (0 regions)")
    # 1. Flatten the tree into parallel lists
    flat_nodes: list[_FlatNode] = []
    _flatten_resource(resource, flat_nodes)

    # 2. Build name hash table
    name_table = _NameTable()
    for fn in flat_nodes:
        name_table.add(fn.id)
        if fn.key_attribute:
            name_table.add(fn.key_attribute)
        for attr in fn.attributes:
            name_table.add(attr.id)

    # 3. Serialize sections
    names_raw = _serialize_names(name_table)
    nodes_raw = _serialize_nodes(flat_nodes, name_table)
    attrs_raw, values_raw = _serialize_attributes(flat_nodes, name_table)
    keys_raw = _serialize_keys(flat_nodes, name_table)

    # 4. Compress sections with LZ4 frame
    names_compressed = _compress_lz4_block(names_raw)
    nodes_compressed = _compress_lz4_frame(nodes_raw)
    attrs_compressed = _compress_lz4_frame(attrs_raw)
    values_compressed = _compress_lz4_frame(values_raw)
    keys_compressed = _compress_lz4_frame(keys_raw)

    # 5. Write header
    _write_u32(out, LSF_SIGNATURE)
    _write_u32(out, WRITE_VERSION)
    _write_i64(out, ENGINE_VERSION)  # v5+ uses i64

    # 6. Write metadata
    _write_u32(out, len(names_raw))
    _write_u32(out, len(names_compressed))
    _write_u32(out, len(keys_raw))         # v6+ keys
    _write_u32(out, len(keys_compressed))
    _write_u32(out, len(nodes_raw))
    _write_u32(out, len(nodes_compressed))
    _write_u32(out, len(attrs_raw))
    _write_u32(out, len(attrs_compressed))
    _write_u32(out, len(values_raw))
    _write_u32(out, len(values_compressed))
    _write_u8(out, COMPRESSION_LZ4)
    _write_u8(out, 0)   # unknown2
    _write_u16(out, 0)  # unknown3
    _write_u32(out, METADATA_FORMAT_KEYS_AND_ADJACENCY)

    # 7. Write compressed section data (order matters — must match reader)
    out.write(names_compressed)
    out.write(nodes_compressed)
    out.write(attrs_compressed)
    out.write(values_compressed)
    out.write(keys_compressed)


# ── Tree flattening ─────────────────────────────────────────────────

class _FlatNode:
    __slots__ = ("id", "parent_index", "key_attribute", "attributes")

    def __init__(self, node_id: str, parent_index: int, key_attribute: str | None,
                 attributes: list[LsxNodeAttribute]) -> None:
        self.id = node_id
        self.parent_index = parent_index
        self.key_attribute = key_attribute
        self.attributes = attributes


def _flatten_resource(resource: LsxResource, flat: list[_FlatNode]) -> None:
    for region in resource.regions:
        # Create a virtual root node for the region
        root_idx = len(flat)
        flat.append(_FlatNode(region.id, -1, None, []))
        for child in region.nodes:
            _flatten_node(child, root_idx, flat)


def _flatten_node(node: LsxNode, parent_index: int, flat: list[_FlatNode]) -> int:
    # Iterative pre-order DFS to avoid recursion-depth limits on deep trees
    root_idx = len(flat)
    stack: list[tuple[LsxNode, int]] = [(node, parent_index)]
    while stack:
        current, par_idx = stack.pop()
        idx = len(flat)
        flat.append(_FlatNode(current.id, par_idx, current.key_attribute, current.attributes))
        # Push children in reverse so left-to-right order is preserved
        for child in reversed(current.children):
            stack.append((child, idx))
    return root_idx


# ── Name hash table ────────────────────────────────────────────────

class _NameTable:
    def __init__(self) -> None:
        self._buckets: list[list[str]] = [[] for _ in range(STRING_HASH_MAP_SIZE)]
        self._cache: dict[str, tuple[int, int]] = {}

    def add(self, name: str) -> tuple[int, int]:
        if name in self._cache:
            return self._cache[name]
        bucket_idx = _fnv1a_hash(name) % STRING_HASH_MAP_SIZE
        bucket = self._buckets[bucket_idx]
        offset = len(bucket)
        bucket.append(name)
        self._cache[name] = (bucket_idx, offset)
        return bucket_idx, offset

    def lookup(self, name: str) -> int:
        bucket, offset = self._cache[name]
        return (bucket << 16) | offset

    @property
    def buckets(self) -> list[list[str]]:
        return self._buckets


def _fnv1a_hash(s: str) -> int:
    h = 0x811C9DC5
    for ch in s:
        h ^= ord(ch)
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


# ── Section serializers ─────────────────────────────────────────────

def _serialize_names(table: _NameTable) -> bytes:
    buf = io.BytesIO()
    _write_u32(buf, len(table.buckets))
    for bucket in table.buckets:
        _write_u16(buf, len(bucket))
        for name in bucket:
            encoded = name.encode("utf-8")
            _write_u16(buf, len(encoded))
            buf.write(encoded)
    return buf.getvalue()


def _serialize_nodes(flat: list[_FlatNode], names: _NameTable) -> bytes:
    # Pre-compute next-sibling map: for each node, the next node with the same parent
    children_of: dict[int, list[int]] = defaultdict(list)
    for i, fn in enumerate(flat):
        children_of[fn.parent_index].append(i)
    next_sibling: dict[int, int] = {}
    for siblings in children_of.values():
        for k in range(len(siblings) - 1):
            next_sibling[siblings[k]] = siblings[k + 1]

    # Pre-compute first_attribute_index per node
    first_attr_index: dict[int, int] = {}
    attr_cursor = 0
    for i, fn in enumerate(flat):
        if fn.attributes:
            first_attr_index[i] = attr_cursor
            attr_cursor += len(fn.attributes)

    buf = io.BytesIO()
    for i, fn in enumerate(flat):
        name_hash = names.lookup(fn.id)
        _write_u32(buf, name_hash)
        _write_i32(buf, fn.parent_index)
        _write_i32(buf, next_sibling.get(i, -1))
        _write_i32(buf, first_attr_index.get(i, -1))
    return buf.getvalue()


def _serialize_attributes(flat: list[_FlatNode], names: _NameTable) -> tuple[bytes, bytes]:
    attr_buf = io.BytesIO()
    val_buf = io.BytesIO()

    global_attr_index = 0
    for fn in flat:
        for i, attr in enumerate(fn.attributes):
            name_hash = names.lookup(attr.id)
            type_id = NAME_TO_TYPE_ID.get(attr.attr_type, 0)

            # Serialize value
            val_start = val_buf.tell()
            _write_typed_value(val_buf, type_id, attr)
            val_end = val_buf.tell()
            length = val_end - val_start

            type_and_length = (length << 6) | (type_id & 0x3F)

            # next_attribute_index
            if i < len(fn.attributes) - 1:
                next_attr = global_attr_index + 1
            else:
                next_attr = -1

            _write_u32(attr_buf, name_hash)
            _write_u32(attr_buf, type_and_length)
            _write_i32(attr_buf, next_attr)
            _write_u32(attr_buf, val_start)

            global_attr_index += 1

    return attr_buf.getvalue(), val_buf.getvalue()


def _serialize_keys(flat: list[_FlatNode], names: _NameTable) -> bytes:
    buf = io.BytesIO()
    for i, fn in enumerate(flat):
        if fn.key_attribute:
            _write_u32(buf, i)
            _write_u32(buf, names.lookup(fn.key_attribute))
    return buf.getvalue()


# ── Typed value encoding ───────────────────────────────────────────

def _write_typed_value(w: BinaryIO, type_id: int, attr: LsxNodeAttribute) -> None:
    v = attr.value

    try:
        _write_typed_value_inner(w, type_id, attr)
    except (ValueError, TypeError, struct.error) as e:
        raise ValueError(
            f"Cannot encode attribute '{attr.id}' (type={attr.attr_type}, value={v!r}): {e}"
        ) from e


def _write_typed_value_inner(w: BinaryIO, type_id: int, attr: LsxNodeAttribute) -> None:
    handler = _WRITE_DISPATCH.get(type_id)
    if handler is not None:
        handler(w, attr)
        return
    raise ValueError(f"Cannot write LSF type id {type_id}")


def _wtv_none(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    pass

def _wtv_u8(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_u8(w, int(attr.value))

def _wtv_i16(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_i16(w, int(attr.value))

def _wtv_u16(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_u16(w, int(attr.value))

def _wtv_i32(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_i32(w, int(attr.value))

def _wtv_u32(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_u32(w, int(attr.value))

def _wtv_f32(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_f32(w, float(attr.value))

def _wtv_f64(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_f64(w, float(attr.value))

def _wtv_ivec(w: BinaryIO, attr: LsxNodeAttribute, *, cols: int) -> None:
    parts = attr.value.split()
    while len(parts) < cols:
        parts.append("0")
    w.write(struct.pack(f"<{cols}i", *(int(p) for p in parts[:cols])))

def _wtv_fvec(w: BinaryIO, attr: LsxNodeAttribute, *, cols: int) -> None:
    parts = attr.value.split()
    while len(parts) < cols:
        parts.append("0")
    w.write(struct.pack(f"<{cols}f", *(float(p) for p in parts[:cols])))

def _wtv_matrix(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    parts = attr.value.split()
    w.write(struct.pack(f"<{len(parts)}f", *(float(p) for p in parts)))

def _wtv_bool(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_u8(w, 1 if attr.value == "True" else 0)

def _wtv_string(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    w.write(attr.value.encode("utf-8") + b"\x00")

def _wtv_u64(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_u64(w, int(attr.value))

def _wtv_scratch(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    w.write(bytes.fromhex(attr.value))

def _wtv_i64(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_i64(w, int(attr.value))

def _wtv_i8(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    _write_i8(w, int(attr.value))

def _wtv_translated(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    handle = attr.handle or attr.value
    ver = attr.version or 1
    _write_translated_string(w, handle, ver)

def _wtv_wide(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    w.write(attr.value.encode("utf-8") + b"\x00")

def _wtv_guid(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    v = attr.value
    if not v or v.isspace():
        _write_lsf_guid(w, uuid.UUID(int=0))
    else:
        try:
            _write_lsf_guid(w, uuid.UUID(v))
        except ValueError:
            _output.warnings.warn(f"Invalid GUID '{v}' for attribute '{attr.id}' -- writing nil")
            _write_lsf_guid(w, uuid.UUID(int=0))

def _wtv_translated_fs(w: BinaryIO, attr: LsxNodeAttribute) -> None:
    handle = attr.handle or attr.value
    ver = attr.version or 1
    _write_translated_string(w, handle, ver)
    _write_i32(w, len(attr.arguments))
    for arg in attr.arguments:
        key_bytes = arg.key.encode("utf-8")
        _write_i32(w, len(key_bytes))
        w.write(key_bytes)
        nested_handle = arg.string.handle or arg.string.value
        nested_ver = arg.string.version or 1
        _write_translated_string(w, nested_handle, nested_ver)
        val_bytes = arg.value.encode("utf-8")
        _write_i32(w, len(val_bytes))
        w.write(val_bytes)


from functools import partial

_WRITE_DISPATCH: dict[int, ...] = {
    0: _wtv_none,
    1: _wtv_u8,
    2: _wtv_i16,
    3: _wtv_u16,
    4: _wtv_i32,
    5: _wtv_u32,
    6: _wtv_f32,
    7: _wtv_f64,
    8: partial(_wtv_ivec, cols=2),
    9: partial(_wtv_ivec, cols=3),
    10: partial(_wtv_ivec, cols=4),
    11: partial(_wtv_fvec, cols=2),
    12: partial(_wtv_fvec, cols=3),
    13: partial(_wtv_fvec, cols=4),
    14: _wtv_matrix,
    15: _wtv_matrix,
    16: _wtv_matrix,
    17: _wtv_matrix,
    18: _wtv_matrix,
    19: _wtv_bool,
    20: _wtv_string,
    21: _wtv_string,
    22: _wtv_string,
    23: _wtv_string,
    24: _wtv_u64,
    25: _wtv_scratch,
    26: _wtv_i64,
    27: _wtv_i8,
    28: _wtv_translated,
    29: _wtv_wide,
    30: _wtv_wide,
    31: _wtv_guid,
    32: _wtv_i64,
    33: _wtv_translated_fs,
}


def _write_translated_string(w: BinaryIO, handle: str, version: int) -> None:
    _write_u16(w, version)
    encoded = handle.encode("utf-8") + b"\x00"
    _write_i32(w, len(encoded))
    w.write(encoded)


def _write_lsf_guid(w: BinaryIO, g: uuid.UUID) -> None:
    raw = bytearray(g.bytes_le)
    for i in range(8, 16, 2):
        raw[i], raw[i + 1] = raw[i + 1], raw[i]
    w.write(bytes(raw))


# ── Compression ─────────────────────────────────────────────────────

def _compress_lz4_frame(data: bytes) -> bytes:
    if not data:
        return b""
    return lz4.frame.compress(data)


def _compress_lz4_block(data: bytes) -> bytes:
    """LZ4 block compression for the names section (allow_chunked=False on read)."""
    if not data:
        return b""
    return lz4.block.compress(data, store_size=False)


# ── Primitive writers ───────────────────────────────────────────────

def _write_u8(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<B", v))


def _write_i8(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<b", v))


def _write_u16(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<H", v))


def _write_i16(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<h", v))


def _write_u32(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<I", v))


def _write_i32(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<i", v))


def _write_u64(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<Q", v))


def _write_i64(w: BinaryIO, v: int) -> None:
    w.write(struct.pack("<q", v))


def _write_f32(w: BinaryIO, v: float) -> None:
    w.write(struct.pack("<f", v))


def _write_f64(w: BinaryIO, v: float) -> None:
    w.write(struct.pack("<d", v))
