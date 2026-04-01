"""LSX data model — mirrors the Rust LsxResource / LsxNode / LsxNodeAttribute hierarchy."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class LsxTranslatedFsArgument:
    key: str
    string: LsxNodeAttribute
    value: str


@dataclass(slots=True)
class LsxNodeAttribute:
    id: str
    attr_type: str
    value: str
    handle: str | None = None
    version: int | None = None
    arguments: list[LsxTranslatedFsArgument] = field(default_factory=list)


@dataclass(slots=True)
class LsxNode:
    id: str
    key_attribute: str | None = None
    attributes: list[LsxNodeAttribute] = field(default_factory=list)
    children: list[LsxNode] = field(default_factory=list)
    _attr_cache: dict[str, LsxNodeAttribute] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def attr(self, name: str) -> LsxNodeAttribute | None:
        cache = self._attr_cache
        if cache is None:
            cache = {a.id: a for a in self.attributes}
            self._attr_cache = cache
        return cache.get(name)

    def attr_value(self, name: str, default: str = "") -> str:
        a = self.attr(name)
        return a.value if a else default

    def children_with_id(self, node_id: str) -> list[LsxNode]:
        return [c for c in self.children if c.id == node_id]


@dataclass
class LsxRegion:
    id: str
    nodes: list[LsxNode] = field(default_factory=list)


@dataclass
class LsxResource:
    regions: list[LsxRegion] = field(default_factory=list)

    def region(self, region_id: str) -> LsxRegion | None:
        for r in self.regions:
            if r.id == region_id:
                return r
        return None


# ── LSF type mappings ───────────────────────────────────────────────

TYPE_ID_TO_NAME: dict[int, str] = {
    0: "None",
    1: "uint8",
    2: "int16",
    3: "uint16",
    4: "int32",
    5: "uint32",
    6: "float",
    7: "double",
    8: "ivec2",
    9: "ivec3",
    10: "ivec4",
    11: "fvec2",
    12: "fvec3",
    13: "fvec4",
    14: "mat2x2",
    15: "mat3x3",
    16: "mat3x4",
    17: "mat4x3",
    18: "mat4x4",
    19: "bool",
    20: "string",
    21: "path",
    22: "FixedString",
    23: "LSString",
    24: "uint64",
    25: "ScratchBuffer",
    26: "old_int64",
    27: "int8",
    28: "TranslatedString",
    29: "WString",
    30: "LSWString",
    31: "guid",
    32: "int64",
    33: "TranslatedFSString",
}

NAME_TO_TYPE_ID: dict[str, int] = {v: k for k, v in TYPE_ID_TO_NAME.items()}

# ── LSF binary constants ────────────────────────────────────────────

LSF_SIGNATURE = int.from_bytes(b"LSOF", "little")
