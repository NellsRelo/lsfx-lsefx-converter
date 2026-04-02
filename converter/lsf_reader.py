"""LSF binary reader — parses .lsf / .lsfx binary files into LsxResource.

Port of the Rust parser at src-tauri/src/parsers/lsf.rs.
"""

import io
import struct
import uuid
import zlib
from enum import IntEnum
from typing import BinaryIO

import lz4.block
import lz4.frame

from .lsx_model import (
    TYPE_ID_TO_NAME,
    LSF_SIGNATURE,
    LsxNode,
    LsxNodeAttribute,
    LsxRegion,
    LsxResource,
    LsxTranslatedFsArgument,
)
from .errors import LsfParseError

# ── Constants ───────────────────────────────────────────────────────

MIN_VERSION = 2
MAX_VERSION = 7
NAME_HASH_BUCKET_LIMIT = 4096
MAX_SECTION_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024  # cumulative across all sections in one file

# Pre-compiled struct formats for batch node/attribute parsing
_NODE_LONG_FMT = struct.Struct("<Iiii")   # 16B: name_hash(u32), parent(i32), next_sibling(i32), first_attr(i32)
_NODE_SHORT_FMT = struct.Struct("<Iii")   # 12B: name_hash(u32), first_attr(i32), parent(i32)
_ATTR_V2_FMT = struct.Struct("<IIi")     # 12B: name_hash(u32), type_and_length(u32), node_index(i32)
_ATTR_V3_FMT = struct.Struct("<IIiI")    # 16B: name_hash(u32), type_and_length(u32), next_attr(i32), data_offset(u32)


class Compression(IntEnum):
    NONE = 0
    ZLIB = 1
    LZ4 = 2


class MetadataFormat(IntEnum):
    NONE = 0
    KEYS_AND_ADJACENCY = 1
    NONE2 = 2


# ── Internal structs ────────────────────────────────────────────────

class _LsfMetadata:
    __slots__ = (
        "strings_uncompressed", "strings_on_disk",
        "keys_uncompressed", "keys_on_disk",
        "nodes_uncompressed", "nodes_on_disk",
        "attrs_uncompressed", "attrs_on_disk",
        "values_uncompressed", "values_on_disk",
        "compression_flags", "metadata_format",
    )

    def __init__(self) -> None:
        self.strings_uncompressed = 0
        self.strings_on_disk = 0
        self.keys_uncompressed = 0
        self.keys_on_disk = 0
        self.nodes_uncompressed = 0
        self.nodes_on_disk = 0
        self.attrs_uncompressed = 0
        self.attrs_on_disk = 0
        self.values_uncompressed = 0
        self.values_on_disk = 0
        self.compression_flags: int = 0
        self.metadata_format: int = 0


class _NodeInfo:
    __slots__ = ("parent_index", "name_bucket", "name_offset", "first_attr_index", "key_attribute")

    def __init__(self, parent_index: int, name_bucket: int, name_offset: int, first_attr_index: int) -> None:
        self.parent_index = parent_index
        self.name_bucket = name_bucket
        self.name_offset = name_offset
        self.first_attr_index = first_attr_index
        self.key_attribute: str | None = None


class _AttrInfo:
    __slots__ = ("name_bucket", "name_offset", "type_id", "length", "data_offset", "next_attr_index")

    def __init__(self, name_bucket: int, name_offset: int, type_id: int, length: int,
                 data_offset: int, next_attr_index: int) -> None:
        self.name_bucket = name_bucket
        self.name_offset = name_offset
        self.type_id = type_id
        self.length = length
        self.data_offset = data_offset
        self.next_attr_index = next_attr_index


# ── Public API ──────────────────────────────────────────────────────

def read_lsf(source: str | BinaryIO) -> LsxResource:
    """Parse an LSF/LSFX binary file and return an LsxResource tree.

    *source* may be a file path or a readable binary stream.
    """
    if isinstance(source, str):
        try:
            with open(source, "rb") as fh:
                return _parse(fh)
        except FileNotFoundError:
            raise FileNotFoundError(f"LSF file not found: {source}") from None
    return _parse(source)


# ── Parser core ─────────────────────────────────────────────────────

def _parse(r: BinaryIO) -> LsxResource:
    sig = _read_u32(r)
    if sig != LSF_SIGNATURE:
        raise LsfParseError(f"Bad LSF signature: expected 0x{LSF_SIGNATURE:08X}, got 0x{sig:08X}")

    version = _read_u32(r)
    if not (MIN_VERSION <= version <= MAX_VERSION):
        raise LsfParseError(f"Unsupported LSF version {version}")

    # engine version (unused, but must be consumed)
    if version >= 5:
        _read_i64(r)
    else:
        _read_i32(r)

    meta = _read_metadata(r, version)

    total_allocated = 0

    names_raw = _read_section(r, meta.strings_on_disk, meta.strings_uncompressed,
                              meta.compression_flags, allow_chunked=False)
    total_allocated += len(names_raw)
    names = _parse_names(names_raw)

    nodes_raw = _read_section(r, meta.nodes_on_disk, meta.nodes_uncompressed,
                              meta.compression_flags, allow_chunked=True)
    total_allocated += len(nodes_raw)
    has_adjacency = version >= 3 and meta.metadata_format == MetadataFormat.KEYS_AND_ADJACENCY
    nodes = _parse_nodes(nodes_raw, names, has_adjacency)

    attrs_raw = _read_section(r, meta.attrs_on_disk, meta.attrs_uncompressed,
                              meta.compression_flags, allow_chunked=True)
    total_allocated += len(attrs_raw)
    if has_adjacency:
        attrs = _parse_attrs_v3(attrs_raw, names)
    else:
        attrs = _parse_attrs_v2(attrs_raw, names)

    values = _read_section(r, meta.values_on_disk, meta.values_uncompressed,
                           meta.compression_flags, allow_chunked=True)
    total_allocated += len(values)

    if total_allocated > MAX_TOTAL_BYTES:
        raise LsfParseError(
            f"Cumulative section size {total_allocated} exceeds {MAX_TOTAL_BYTES} byte limit"
        )

    if meta.metadata_format == MetadataFormat.KEYS_AND_ADJACENCY:
        keys_raw = _read_section(r, meta.keys_on_disk, meta.keys_uncompressed,
                                 meta.compression_flags, allow_chunked=True)
        total_allocated += len(keys_raw)
        if total_allocated > MAX_TOTAL_BYTES:
            raise LsfParseError(
                f"Cumulative section size {total_allocated} exceeds {MAX_TOTAL_BYTES} byte limit"
            )
        _apply_keys(keys_raw, names, nodes)

    return _build_resource(names, nodes, attrs, values)


# ── Metadata ────────────────────────────────────────────────────────

def _read_metadata(r: BinaryIO, version: int) -> _LsfMetadata:
    m = _LsfMetadata()
    m.strings_uncompressed = _read_u32(r)
    m.strings_on_disk = _read_u32(r)

    if version >= 6:
        m.keys_uncompressed = _read_u32(r)
        m.keys_on_disk = _read_u32(r)

    m.nodes_uncompressed = _read_u32(r)
    m.nodes_on_disk = _read_u32(r)
    m.attrs_uncompressed = _read_u32(r)
    m.attrs_on_disk = _read_u32(r)
    m.values_uncompressed = _read_u32(r)
    m.values_on_disk = _read_u32(r)
    m.compression_flags = _read_u8(r)
    _read_u8(r)   # unknown2
    _read_u16(r)  # unknown3
    m.metadata_format = _read_u32(r)
    return m


# ── Section decompression ──────────────────────────────────────────

def _read_section(r: BinaryIO, on_disk: int, uncompressed: int,
                  compression_flags: int, *, allow_chunked: bool) -> bytes:
    if uncompressed > MAX_SECTION_BYTES:
        raise LsfParseError(f"LSF section too large: {uncompressed} bytes")
    if on_disk > MAX_SECTION_BYTES:
        raise LsfParseError(f"LSF compressed section too large: {on_disk} bytes")

    if on_disk == 0 and uncompressed == 0:
        return b""

    if on_disk == 0:
        return _read_exact(r, uncompressed)

    raw = _read_exact(r, on_disk)
    method = Compression(compression_flags & 0x0F)

    if method == Compression.NONE:
        return raw

    try:
        if method == Compression.ZLIB:
            result = zlib.decompress(raw)
        elif method == Compression.LZ4:
            if allow_chunked:
                result = lz4.frame.decompress(raw)
            else:
                return lz4.block.decompress(raw, uncompressed_size=uncompressed)
        else:
            raise LsfParseError(f"Unsupported LSF compression method {compression_flags}")
    except (zlib.error, RuntimeError) as e:
        raise LsfParseError(f"Decompression failed ({method.name}): file may be corrupted — {e}") from e

    if len(result) > MAX_SECTION_BYTES:
        raise LsfParseError(f"Decompressed section too large: {len(result)} bytes")
    if uncompressed != 0 and len(result) != uncompressed:
        raise LsfParseError(f"Decompressed size mismatch: expected {uncompressed}, got {len(result)}")
    return result


# ── Name hash table ────────────────────────────────────────────────

def _parse_names(data: bytes) -> list[list[str]]:
    r = io.BytesIO(data)
    bucket_count = _read_u32(r)
    if bucket_count > NAME_HASH_BUCKET_LIMIT:
        raise LsfParseError(f"LSF name bucket count too large: {bucket_count}")

    names: list[list[str]] = []
    for _ in range(bucket_count):
        string_count = _read_u16(r)
        bucket: list[str] = []
        for _ in range(string_count):
            length = _read_u16(r)
            raw = _read_exact(r, length)
            bucket.append(raw.decode("utf-8"))
        names.append(bucket)
    return names


# ── Nodes ───────────────────────────────────────────────────────────

def _parse_nodes(data: bytes, names: list[list[str]], long_nodes: bool) -> list[_NodeInfo]:
    fmt = _NODE_LONG_FMT if long_nodes else _NODE_SHORT_FMT
    entry_size = fmt.size
    count = len(data) // entry_size
    nodes: list[_NodeInfo] = []
    for i in range(count):
        vals = fmt.unpack_from(data, i * entry_size)
        if long_nodes:
            name_hash, parent, _, first_attr = vals
        else:
            name_hash, first_attr, parent = vals
        bucket, offset = _split_name_hash(name_hash)
        nodes.append(_NodeInfo(parent, bucket, offset, first_attr))
    return nodes


# ── Attributes ──────────────────────────────────────────────────────

def _parse_attrs_v2(data: bytes, names: list[list[str]]) -> list[_AttrInfo]:
    fmt = _ATTR_V2_FMT
    entry_size = fmt.size
    count = len(data) // entry_size
    attrs: list[_AttrInfo] = []
    prev_refs: list[int] = []
    data_offset = 0

    for i in range(count):
        name_hash, type_and_length, node_index = fmt.unpack_from(data, i * entry_size)

        bucket, offset = _split_name_hash(name_hash)

        type_id = type_and_length & 0x3F
        length = type_and_length >> 6
        index = len(attrs)

        info = _AttrInfo(bucket, offset, type_id, length, data_offset, -1)

        chain = node_index + 1
        if chain < 0 or node_index >= 1_000_000:
            raise LsfParseError(f"Invalid node index {node_index}")
        while len(prev_refs) <= chain:
            prev_refs.append(-1)
        if prev_refs[chain] != -1:
            attrs[prev_refs[chain]].next_attr_index = index
        prev_refs[chain] = index

        data_offset += length
        attrs.append(info)
    return attrs


def _parse_attrs_v3(data: bytes, names: list[list[str]]) -> list[_AttrInfo]:
    fmt = _ATTR_V3_FMT
    entry_size = fmt.size
    count = len(data) // entry_size
    attrs: list[_AttrInfo] = []

    for i in range(count):
        name_hash, type_and_length, next_attr, data_offset = fmt.unpack_from(data, i * entry_size)

        bucket, offset = _split_name_hash(name_hash)

        attrs.append(_AttrInfo(
            bucket, offset,
            type_and_length & 0x3F,
            type_and_length >> 6,
            data_offset,
            next_attr,
        ))
    return attrs


# ── Keys ────────────────────────────────────────────────────────────

def _apply_keys(data: bytes, names: list[list[str]], nodes: list[_NodeInfo]) -> None:
    r = io.BytesIO(data)
    while True:
        chunk = r.read(4)
        if len(chunk) < 4:
            break
        node_index = int.from_bytes(chunk, "little")
        key_hash = _read_u32(r)
        bucket, offset = _split_name_hash(key_hash)
        key_name = _resolve_name(names, bucket, offset)
        if node_index < len(nodes):
            nodes[node_index].key_attribute = key_name


# ── Tree construction ───────────────────────────────────────────────

def _build_resource(names: list[list[str]], nodes: list[_NodeInfo],
                    attrs: list[_AttrInfo], values: bytes) -> LsxResource:
    # Build arena of flat nodes — use a shared BytesIO to avoid per-attribute allocation
    values_stream = io.BytesIO(values)
    values_len = len(values)
    arena: list[dict] = []
    for ni in nodes:
        node_id = _resolve_name(names, ni.name_bucket, ni.name_offset)
        node_attrs = _read_node_attrs(names, ni, attrs, values_stream, values_len)
        arena.append({
            "id": node_id,
            "key_attribute": ni.key_attribute,
            "attributes": node_attrs,
            "children": [],
        })

    region_roots: list[int] = []
    for i, ni in enumerate(nodes):
        if ni.parent_index < 0:
            region_roots.append(i)
        elif ni.parent_index < len(arena):
            arena[ni.parent_index]["children"].append(i)
        else:
            raise LsfParseError(f"Node {i} has invalid parent_index {ni.parent_index}")

    def _to_lsx_node(root_idx: int) -> LsxNode:
        # Iterative post-order DFS to avoid recursion-depth limits on deep trees
        order: list[int] = []
        stack = [root_idx]
        while stack:
            idx = stack.pop()
            order.append(idx)
            for c in arena[idx]["children"]:
                stack.append(c)
        built: dict[int, LsxNode] = {}
        for idx in reversed(order):
            entry = arena[idx]
            built[idx] = LsxNode(
                id=entry["id"],
                key_attribute=entry["key_attribute"],
                attributes=entry["attributes"],
                children=[built[c] for c in entry["children"]],
            )
        return built[root_idx]

    regions: list[LsxRegion] = []
    for root_idx in region_roots:
        root = _to_lsx_node(root_idx)
        # If the root node is literally "root", its children become the region's nodes
        if root.id == "root":
            region_nodes = list(root.children)
        else:
            region_nodes = [root]
        regions.append(LsxRegion(id=arena[root_idx]["id"], nodes=region_nodes))

    return LsxResource(regions=regions)


def _read_node_attrs(names: list[list[str]], node: _NodeInfo,
                     attrs: list[_AttrInfo], values_stream: BinaryIO,
                     values_len: int) -> list[LsxNodeAttribute]:
    result: list[LsxNodeAttribute] = []
    visited: set[int] = set()
    next_idx = node.first_attr_index
    while next_idx != -1:
        if next_idx in visited:
            raise LsfParseError(f"Cycle in attribute chain at index {next_idx}")
        if next_idx < 0 or next_idx >= len(attrs):
            raise LsfParseError(f"Attribute index {next_idx} out of range (0..{len(attrs)})")
        visited.add(next_idx)
        info = attrs[next_idx]
        result.append(_read_attr_value(names, info, values_stream, values_len))
        next_idx = info.next_attr_index
    return result


def _read_attr_value(names: list[list[str]], info: _AttrInfo,
                     values_stream: BinaryIO, values_len: int) -> LsxNodeAttribute:
    start = info.data_offset
    end = start + info.length
    if end > values_len:
        raise LsfParseError(f"Attribute value out of bounds: {start}..{end} (len={values_len})")

    values_stream.seek(start)
    attr_name = _resolve_name(names, info.name_bucket, info.name_offset)
    type_name = TYPE_ID_TO_NAME.get(info.type_id)
    if type_name is None:
        raise LsfParseError(f"Unknown LSF type id {info.type_id}")

    value, handle, version, arguments = _read_typed_value(values_stream, info.type_id, info.length)
    return LsxNodeAttribute(
        id=attr_name,
        attr_type=type_name,
        value=value,
        handle=handle,
        version=version,
        arguments=arguments,
    )


# ── Typed value decoding ───────────────────────────────────────────

_ValResult = tuple[str, str | None, int | None, list[LsxTranslatedFsArgument]]
_EMPTY_ARGS: list[LsxTranslatedFsArgument] = []

_IVEC_COLS = {8: 2, 9: 3, 10: 4}
_FVEC_COLS = {11: 2, 12: 3, 13: 4}
_MATRIX_DIMS = {14: (2, 2), 15: (3, 3), 16: (3, 4), 17: (4, 3), 18: (4, 4)}


def _read_typed_value(r: BinaryIO, type_id: int, length: int) -> _ValResult:
    handler = _READ_DISPATCH.get(type_id)
    if handler is not None:
        return handler(r, length)
    raise LsfParseError(f"Unsupported LSF type id {type_id}")


def _rtv_none(r: BinaryIO, length: int) -> _ValResult:
    return ("", None, None, _EMPTY_ARGS)

def _rtv_u8(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_u8(r)), None, None, _EMPTY_ARGS)

def _rtv_i16(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_i16(r)), None, None, _EMPTY_ARGS)

def _rtv_u16(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_u16(r)), None, None, _EMPTY_ARGS)

def _rtv_i32(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_i32(r)), None, None, _EMPTY_ARGS)

def _rtv_u32(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_u32(r)), None, None, _EMPTY_ARGS)

def _rtv_f32(r: BinaryIO, length: int) -> _ValResult:
    return (_fmt_f32(_read_f32(r)), None, None, _EMPTY_ARGS)

def _rtv_f64(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_f64(r)), None, None, _EMPTY_ARGS)

def _rtv_ivec(r: BinaryIO, length: int, *, cols: int) -> _ValResult:
    vals = struct.unpack(f"<{cols}i", _read_exact(r, 4 * cols))
    return (" ".join(str(v) for v in vals), None, None, _EMPTY_ARGS)

def _rtv_fvec(r: BinaryIO, length: int, *, cols: int) -> _ValResult:
    vals = struct.unpack(f"<{cols}f", _read_exact(r, 4 * cols))
    return (" ".join(_fmt_f32(v) for v in vals), None, None, _EMPTY_ARGS)

def _rtv_matrix(r: BinaryIO, length: int, *, total: int) -> _ValResult:
    vals = struct.unpack(f"<{total}f", _read_exact(r, 4 * total))
    return (" ".join(_fmt_f32(v) for v in vals), None, None, _EMPTY_ARGS)

def _rtv_bool(r: BinaryIO, length: int) -> _ValResult:
    return ("True" if _read_u8(r) != 0 else "False", None, None, _EMPTY_ARGS)

def _rtv_string(r: BinaryIO, length: int) -> _ValResult:
    return (_read_lsf_utf8(r, length), None, None, _EMPTY_ARGS)

def _rtv_u64(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_u64(r)), None, None, _EMPTY_ARGS)

def _rtv_scratch(r: BinaryIO, length: int) -> _ValResult:
    return (_read_exact(r, length).hex(), None, None, _EMPTY_ARGS)

def _rtv_i64(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_i64(r)), None, None, _EMPTY_ARGS)

def _rtv_i8(r: BinaryIO, length: int) -> _ValResult:
    return (str(_read_i8(r)), None, None, _EMPTY_ARGS)

def _rtv_translated(r: BinaryIO, length: int) -> _ValResult:
    handle, ver = _read_translated_string(r)
    return (handle, handle, ver, _EMPTY_ARGS)

def _rtv_wide(r: BinaryIO, length: int) -> _ValResult:
    return (_read_lsf_wide_string(r, length), None, None, _EMPTY_ARGS)

def _rtv_guid(r: BinaryIO, length: int) -> _ValResult:
    raw = _read_exact(r, 16)
    return (str(_read_lsf_guid(raw)), None, None, _EMPTY_ARGS)

def _rtv_translated_fs(r: BinaryIO, length: int) -> _ValResult:
    handle, ver = _read_translated_string(r)
    arg_count = max(0, _read_i32(r))
    if arg_count > 10_000:
        raise LsfParseError(f"TranslatedFSString argument count too large: {arg_count}")
    arguments: list[LsxTranslatedFsArgument] = []
    for _ in range(arg_count):
        key_len = max(0, _read_i32(r))
        key = _read_exact(r, key_len).decode("utf-8").rstrip("\0")
        nested_handle, nested_ver = _read_translated_string(r)
        val_len = max(0, _read_i32(r))
        val = _read_exact(r, val_len).decode("utf-8").rstrip("\0")
        arguments.append(LsxTranslatedFsArgument(
            key=key,
            string=LsxNodeAttribute(
                id="", attr_type="TranslatedString",
                value=nested_handle, handle=nested_handle, version=nested_ver),
            value=val,
        ))
    return (handle, handle, ver, arguments)


from functools import partial

_READ_DISPATCH: dict[int, ...] = {
    0: _rtv_none,
    1: _rtv_u8,
    2: _rtv_i16,
    3: _rtv_u16,
    4: _rtv_i32,
    5: _rtv_u32,
    6: _rtv_f32,
    7: _rtv_f64,
    8: partial(_rtv_ivec, cols=2),
    9: partial(_rtv_ivec, cols=3),
    10: partial(_rtv_ivec, cols=4),
    11: partial(_rtv_fvec, cols=2),
    12: partial(_rtv_fvec, cols=3),
    13: partial(_rtv_fvec, cols=4),
    14: partial(_rtv_matrix, total=4),
    15: partial(_rtv_matrix, total=9),
    16: partial(_rtv_matrix, total=12),
    17: partial(_rtv_matrix, total=12),
    18: partial(_rtv_matrix, total=16),
    19: _rtv_bool,
    20: _rtv_string,
    21: _rtv_string,
    22: _rtv_string,
    23: _rtv_string,
    24: _rtv_u64,
    25: _rtv_scratch,
    26: _rtv_i64,
    27: _rtv_i8,
    28: _rtv_translated,
    29: _rtv_wide,
    30: _rtv_wide,
    31: _rtv_guid,
    32: _rtv_i64,
    33: _rtv_translated_fs,
}


# ── String helpers ──────────────────────────────────────────────────

def _read_lsf_utf8(r: BinaryIO, length: int) -> str:
    raw = _read_exact(r, length)
    return raw.rstrip(b"\x00").decode("utf-8")


def _read_lsf_wide_string(r: BinaryIO, length: int) -> str:
    raw = _read_exact(r, length)
    # Heuristic: detect UTF-16LE by checking for null high-bytes
    if len(raw) % 2 == 0 and len(raw) > 0:
        looks_utf16 = any(raw[i + 1] == 0 for i in range(0, len(raw), 2))
        if looks_utf16:
            try:
                return raw.decode("utf-16-le").rstrip("\0")
            except UnicodeDecodeError as e:
                raise LsfParseError(f"Invalid UTF-16LE wide string ({length} bytes): {e}") from e
    try:
        return raw.rstrip(b"\x00").decode("utf-8")
    except UnicodeDecodeError as e:
        raise LsfParseError(f"Invalid UTF-8 fallback for wide string ({length} bytes): {e}") from e


def _read_translated_string(r: BinaryIO) -> tuple[str, int]:
    version = _read_u16(r)
    handle_len = max(0, _read_i32(r))
    handle = _read_lsf_utf8(r, handle_len)
    return handle, version


def _read_lsf_guid(data: bytes) -> uuid.UUID:
    if len(data) != 16:
        raise LsfParseError(f"Invalid GUID byte length {len(data)}")
    swapped = bytearray(data)
    for i in range(8, 16, 2):
        swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
    return uuid.UUID(bytes_le=bytes(swapped))


# ── Name helpers ────────────────────────────────────────────────────

def _split_name_hash(raw: int) -> tuple[int, int]:
    return (raw >> 16, raw & 0xFFFF)


def _resolve_name(names: list[list[str]], bucket: int, offset: int) -> str:
    if bucket >= len(names) or offset >= len(names[bucket]):
        raise LsfParseError(f"Missing name entry {bucket}/{offset}")
    return names[bucket][offset]


# ── Primitive readers ───────────────────────────────────────────────

def _read_exact(r: BinaryIO, n: int) -> bytes:
    data = r.read(n)
    if len(data) != n:
        raise LsfParseError(f"Unexpected EOF: wanted {n} bytes, got {len(data)}")
    return data


def _read_u8(r: BinaryIO) -> int:
    return _read_exact(r, 1)[0]


def _read_i8(r: BinaryIO) -> int:
    return struct.unpack("<b", _read_exact(r, 1))[0]


def _read_u16(r: BinaryIO) -> int:
    return struct.unpack("<H", _read_exact(r, 2))[0]


def _read_i16(r: BinaryIO) -> int:
    return struct.unpack("<h", _read_exact(r, 2))[0]


def _read_u32(r: BinaryIO) -> int:
    return struct.unpack("<I", _read_exact(r, 4))[0]


def _read_i32(r: BinaryIO) -> int:
    return struct.unpack("<i", _read_exact(r, 4))[0]


def _read_u64(r: BinaryIO) -> int:
    return struct.unpack("<Q", _read_exact(r, 8))[0]


def _read_i64(r: BinaryIO) -> int:
    return struct.unpack("<q", _read_exact(r, 8))[0]


def _read_f32(r: BinaryIO) -> float:
    return struct.unpack("<f", _read_exact(r, 4))[0]


def _read_f64(r: BinaryIO) -> float:
    return struct.unpack("<d", _read_exact(r, 8))[0]


def _fmt_f32(val: float) -> str:
    """Format a float32 value with minimal decimal places that round-trip.

    Finds the shortest decimal representation such that packing back to
    float32 yields the same binary value.  Strips trailing zeros and the
    decimal point when the result is an integer (e.g. ``0`` not ``0.0``).
    """
    packed = struct.pack("<f", val)
    for decimals in range(0, 10):
        formatted = f"{val:.{decimals}f}"
        if struct.pack("<f", float(formatted)) == packed:
            if "." in formatted:
                formatted = formatted.rstrip("0").rstrip(".")
            return formatted
    return str(val)
