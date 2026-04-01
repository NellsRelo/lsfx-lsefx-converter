# LSF Binary Format Reference

> Reverse-engineered from BG3 `.lsfx` files and the LSlib reference
> implementation. This document describes version 6 (the version this tool
> writes), but the reader supports versions 2–7.

## File Layout

```
┌─────────────── Header ──────────────────┐
│  Signature        u32  "LSOF" LE        │
│  Version          u32  (2‥7)            │
│  EngineVersion    i64  (v5+) or i32     │
├─────────────── Metadata ────────────────┤
│  StringsUncompressed   u32              │
│  StringsOnDisk         u32              │
│  KeysUncompressed      u32  (v6+)       │
│  KeysOnDisk            u32  (v6+)       │
│  NodesUncompressed     u32              │
│  NodesOnDisk           u32              │
│  AttrsUncompressed     u32              │
│  AttrsOnDisk           u32              │
│  ValuesUncompressed    u32              │
│  ValuesOnDisk          u32              │
│  CompressionFlags      u8               │
│  Unknown2              u8               │
│  Unknown3              u16              │
│  MetadataFormat        u32              │
├─────────── Compressed sections ─────────┤
│  Names section    (LZ4 block)           │
│  Nodes section    (LZ4 frame)           │
│  Attrs section    (LZ4 frame)           │
│  Values section   (LZ4 frame)           │
│  Keys section     (LZ4 frame, v6+)      │
└─────────────────────────────────────────┘
```

## Compression

The lower 4 bits of `CompressionFlags` select the algorithm:

| Value | Method |
|-------|--------|
| 0     | None   |
| 1     | Zlib   |
| 2     | LZ4    |

The **Names** section uses LZ4 *block* compression (single shot).
All other sections use LZ4 *frame* compression (chunked / streaming).

## Names Section

A hash-map of strings used as node IDs and attribute names.

```
BucketCount   u32
For each bucket:
    StringCount   u16
    For each string:
        Length    u16      (UTF-8 byte count)
        Data     [u8]     (UTF-8, no null terminator)
```

Names are referenced elsewhere via a packed `u32`:
- **bits [31:16]** → bucket index
- **bits [15:0]** → offset within bucket

The hash function is FNV-1a over the UTF-8 codepoints, modulo 512
(`STRING_HASH_MAP_SIZE = 0x200`).

## Nodes Section

Flat list of nodes. Two layouts depending on `MetadataFormat`:

### Long format (v3+, `MetadataFormat = KeysAndAdjacency`)

Each entry is 16 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0      | u32  | NameHash (packed bucket+offset) |
| 4      | i32  | ParentIndex (-1 = region root) |
| 8      | i32  | NextSiblingIndex (-1 = none) |
| 12     | i32  | FirstAttributeIndex (-1 = none) |

### Short format (v2)

Each entry is 12 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0      | u32  | NameHash |
| 4      | i32  | FirstAttributeIndex |
| 8      | i32  | ParentIndex |

## Attributes Section

### v3 format (KeysAndAdjacency)

Each entry is 16 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0      | u32  | NameHash |
| 4      | u32  | TypeAndLength (bits [5:0] = type id, bits [31:6] = byte length) |
| 8      | i32  | NextAttributeIndex (-1 = end of chain) |
| 12     | u32  | DataOffset into Values section |

### v2 format

Each entry is 12 bytes:

| Offset | Type | Field |
|--------|------|-------|
| 0      | u32  | NameHash |
| 4      | u32  | TypeAndLength |
| 8      | i32  | NodeIndex (which node owns this attr, +1 bias) |

Data offsets are implicit (packed sequentially).

## Values Section

A flat byte buffer. Each attribute's value starts at its `DataOffset` and
runs for `Length` bytes. The encoding depends on the type id:

| ID | Name | Size | Encoding |
|----|------|------|----------|
| 0  | None | 0 | — |
| 1  | uint8 | 1 | LE |
| 2  | int16 | 2 | LE |
| 3  | uint16 | 2 | LE |
| 4  | int32 | 4 | LE |
| 5  | uint32 | 4 | LE |
| 6  | float | 4 | IEEE 754 |
| 7  | double | 8 | IEEE 754 |
| 8  | ivec2 | 8 | 2 × i32 |
| 9  | ivec3 | 12 | 3 × i32 |
| 10 | ivec4 | 16 | 4 × i32 |
| 11 | fvec2 | 8 | 2 × f32 |
| 12 | fvec3 | 12 | 3 × f32 |
| 13 | fvec4 | 16 | 4 × f32 |
| 14 | mat2×2 | 16 | 4 × f32 |
| 15 | mat3×3 | 36 | 9 × f32 |
| 16 | mat3×4 | 48 | 12 × f32 |
| 17 | mat4×3 | 48 | 12 × f32 |
| 18 | mat4×4 | 64 | 16 × f32 |
| 19 | bool | 1 | u8 (0 = False) |
| 20 | string | var | Null-terminated UTF-8 |
| 21 | path | var | Null-terminated UTF-8 |
| 22 | FixedString | var | Null-terminated UTF-8 |
| 23 | LSString | var | Null-terminated UTF-8 |
| 24 | uint64 | 8 | LE |
| 25 | ScratchBuffer | var | Raw bytes |
| 26 | old_int64 | 8 | LE |
| 27 | int8 | 1 | LE |
| 28 | TranslatedString | var | See below |
| 29 | WString | var | See "Wide String Heuristic" below |
| 30 | LSWString | var | See "Wide String Heuristic" below |
| 31 | guid | 16 | Mixed-endian (see below) |
| 32 | int64 | 8 | LE |
| 33 | TranslatedFSString | var | See below |

### GUID encoding

The 16-byte GUID uses .NET-style mixed endianness
(matches `System.Guid.ToByteArray()`):

```
Bytes  0‥3   → Data1 (u32 LE)
Bytes  4‥5   → Data2 (u16 LE)
Bytes  6‥7   → Data3 (u16 LE)
Bytes  8‥15  → Data4 (big-endian)
```

### TranslatedString

```
Version     u16
HandleLen   i32
Handle      [u8]   (UTF-8, HandleLen bytes)
```

### TranslatedFSString

```
Version     u16
HandleLen   i32
Handle      [u8]
ArgCount    i32
For each argument:
    KeyLen      i32
    Key         [u8]   (UTF-8)
    Version     u16
    HandleLen   i32
    Handle      [u8]
    ValueLen    i32
    Value       [u8]   (UTF-8)
```

### Wide String Heuristic (types 29, 30)

WString and LSWString are *nominally* UTF-16LE, but modern BG3 files
(Patch 3+) store them as null-terminated UTF-8 — the same encoding as
regular strings. Older files and some edge cases still use true UTF-16LE.

The reader applies a heuristic to distinguish the two encodings at runtime:

1. If the byte count is even and ≥ 2, check whether **any** odd-indexed
   byte is `0x00` (characteristic of ASCII-range UTF-16LE where the
   high byte of each code unit is zero).
2. If the null-byte pattern matches → decode as UTF-16LE.
3. Otherwise → strip trailing null bytes and decode as UTF-8.

This works reliably because pure-ASCII UTF-8 will never have interior
null bytes at odd positions, while UTF-16LE always will for BMP code
points ≤ U+00FF (which covers all Latin-1 property names seen in BG3).

On the write side, the converter always emits null-terminated UTF-8
for wide strings, matching the modern BG3 convention.

## Keys Section (v6+)

Assigns `key_attribute` to nodes. Packed as sequential 8-byte entries:

```
NodeIndex   u32
KeyHash     u32   (packed bucket+offset into Names)
```

## Tree Reconstruction

1. Nodes with `ParentIndex == -1` are region roots.
2. All other nodes are children of their parent.
3. The region root's name becomes the `LsxRegion.id`.
4. If the root is literally named `"root"`, its children are promoted to
   become the region's direct nodes; otherwise the root itself is the
   sole region node.
