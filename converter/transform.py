"""Bidirectional structural transform between LsxResource (runtime .lsfx) and EffectResource (toolkit .lsefx).

Decompile direction:  LsxResource  -> EffectResource  (lsx_to_effect)
Compile direction:    EffectResource -> LsxResource   (effect_to_lsx)

The AllSpark registry provides the GUID <-> property name mapping.
See docs/lsf-format.md for the binary layout and README.md § Format Comparison
for the structural differences between the two representations.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING

from . import _output
from .errors import TransformError

# Private namespace for deterministic UUID-v5 fallbacks (generated once).
_NS_LSFX = uuid.UUID("b7e3f1a0-8c4d-4e6f-9a2b-1d0c5e7f8a9b")

from .effect_model import (
    Component,
    Datum,
    EffectResource,
    Keyframe,
    Property,
    RampChannel,
    RampChannelData,
    Track,
    TrackGroup,
    TrackGroupId,
)
from .lsx_model import LsxNode, LsxNodeAttribute, LsxRegion, LsxResource

if TYPE_CHECKING:
    from .allspark import AllSparkRegistry

# ── Runtime Property Type enum ──────────────────────────────────────
# These match the "Type" attribute on runtime Property nodes in .lsfx.
# Values correspond to the AllSpark editor's internal property kinds.
PROP_TYPE_BOOL = 0       # Boolean checkbox
PROP_TYPE_INT32 = 1      # Integer / IntegerSlider
PROP_TYPE_INT32_RANGE = 2  # IntegerRangeSlider (Min, Max)
PROP_TYPE_COLOR = 3      # Color picker (packed ABGR u32 or fvec4); may be keyframed
PROP_TYPE_FLOAT = 4      # Float / FloatSlider; may be keyframed
PROP_TYPE_RANGE = 5      # FloatRangeSlider (Min, Max)
PROP_TYPE_KEYFRAMED = 6  # Ramp / animation curve (Frames → Channels)
PROP_TYPE_STRING = 7     # Free-form string / FixedString
PROP_TYPE_VECTOR3 = 8    # 3-component vector (space-separated in LSF, comma-separated in .lsefx)
# 9 is unused
PROP_TYPE_RESOURCE = 10  # Asset reference GUID (textures, models, etc.)

# AllSpark type name → runtime Property Type enum
_ALLSPARK_TYPE_MAP: dict[str, int] = {
    "Boolean": PROP_TYPE_BOOL,
    "Float": PROP_TYPE_FLOAT,
    "FloatSlider": PROP_TYPE_FLOAT,
    "FloatRangeSlider": PROP_TYPE_RANGE,
    "Integer": PROP_TYPE_INT32,
    "IntegerSlider": PROP_TYPE_INT32,
    "IntegerRangeSlider": PROP_TYPE_INT32_RANGE,
    "DropDownList": PROP_TYPE_INT32,
    "Vector2": PROP_TYPE_VECTOR3,
    "Vector3": PROP_TYPE_VECTOR3,
    "Vector4": PROP_TYPE_COLOR,
    "Color": PROP_TYPE_COLOR,
    "ColorRamp": PROP_TYPE_COLOR,
    "Ramp": PROP_TYPE_KEYFRAMED,
    "String": PROP_TYPE_STRING,
    "FixedString": PROP_TYPE_STRING,
    "CustomString": PROP_TYPE_RESOURCE,
    "Text": PROP_TYPE_STRING,
    "ShortNameList": PROP_TYPE_RESOURCE,
    "Resource": PROP_TYPE_RESOURCE,
    "Guid": PROP_TYPE_RESOURCE,
    "AnimationSubSet": PROP_TYPE_RESOURCE,
}

# ── FrameType enum ──────────────────────────────────────────────────
FRAME_TYPE_LINEAR = 0
FRAME_TYPE_SPLINE = 1

# Editor-only property GUIDs — these are baked into component attributes
# (Start/End Time) at the binary level and must not be re-emitted as
# Property nodes during compilation.
_EDITOR_ONLY_PROPERTY_GUIDS = {
    "035b5248-d0ca-44b7-853f-3acb84110e67",  # Start / End Time
}


# ── Resource display-name stripping ─────────────────────────────────

_RE_DISPLAY_NAME_GUID = re.compile(r"<([0-9a-fA-F-]{36})>\s*$")


def _int_attr(node: LsxNode, name: str, default: str = "0") -> int:
    raw = node.attr_value(name, default)
    try:
        return int(raw)
    except ValueError:
        raise TransformError(f"Non-integer @{name} on <{node.id}>: {raw!r}")


def _strip_resource_display_name(value: str) -> str:
    """Strip toolkit display-name prefix from resource values.

    Toolkit: "VFX_Material_Foo <f7fc084b-d098-0d9a-8033-1cb61c3beb37>"
    Runtime: "f7fc084b-d098-0d9a-8033-1cb61c3beb37"
    """
    m = _RE_DISPLAY_NAME_GUID.search(value)
    if m:
        return m.group(1)
    return value


# ── Color packing helpers ───────────────────────────────────────────

def _packed_color_to_fvec4(int_str: str) -> str:
    """Convert packed ARGB integer (toolkit) to RGBA fvec4 string (runtime).

    E.g. "-1" (=0xFFFFFFFF) → "1 1 1 1"
    """
    try:
        i = int(int_str)
    except ValueError:
        return "0 0 0 1"
    u = i & 0xFFFFFFFF
    a = ((u >> 24) & 0xFF) / 255.0
    r = ((u >> 16) & 0xFF) / 255.0
    g = ((u >> 8) & 0xFF) / 255.0
    b = (u & 0xFF) / 255.0
    return f"{r} {g} {b} {a}"


def _fvec4_to_packed_color(fvec4_str: str) -> str:
    """Convert RGBA fvec4 string (runtime) to packed ARGB integer (toolkit).

    E.g. "1 1 1 1" → "-1"
    """
    parts = fvec4_str.split()
    while len(parts) < 4:
        parts.append("0")
    r = max(0, min(255, round(float(parts[0]) * 255.0)))
    g = max(0, min(255, round(float(parts[1]) * 255.0)))
    b = max(0, min(255, round(float(parts[2]) * 255.0)))
    a = max(0, min(255, round(float(parts[3]) * 255.0)))
    u = (a << 24) | (r << 16) | (g << 8) | b
    # Convert to signed int32
    if u >= 0x80000000:
        return str(u - 0x100000000)
    return str(u)


# ═══════════════════════════════════════════════════════════════════
#  LsxResource  →  EffectResource  (decompilation / .lsfx → .lsefx)
# ═══════════════════════════════════════════════════════════════════

def lsx_to_effect(resource: LsxResource, registry: AllSparkRegistry) -> EffectResource:
    """Convert a runtime LsxResource (from .lsfx binary) to an EffectResource."""
    effect = EffectResource()

    effect_region = resource.region("Effect")
    if effect_region is None:
        _output.warnings.warn("No 'Effect' region found in LsxResource")
        return effect

    # Collect all EffectComponent nodes from the tree
    all_components: list[LsxNode] = []
    _find_effect_components(effect_region.nodes, all_components)

    if all_components:
        _decompile_into_trackgroups(all_components, effect, registry)

    return effect


def _find_effect_components(nodes: list[LsxNode], result: list[LsxNode]) -> None:
    """Recursively find all EffectComponent nodes."""
    for node in nodes:
        if node.id == "EffectComponent":
            result.append(node)
        elif node.id in ("Effect", "EffectComponents"):
            _find_effect_components(node.children, result)


def _decompile_into_trackgroups(components: list[LsxNode], effect: EffectResource,
                                registry: AllSparkRegistry) -> None:
    """Group runtime EffectComponent nodes by Track index into tracks.

    The runtime format uses a flat @Track uint32 index. Each unique Track value
    becomes one Track inside a single TrackGroup (since trackgroup boundaries
    are not preserved in the runtime format).
    """
    track_map: dict[int, list[LsxNode]] = {}
    for node in components:
        track_idx = _int_attr(node, "Track")
        track_map.setdefault(track_idx, []).append(node)

    tg = TrackGroup(name="New Track Group", ids=[TrackGroupId(value="1")])

    for track_idx in sorted(track_map.keys()):
        track = Track(name="Track")
        for comp_node in track_map[track_idx]:
            track.components.append(_decompile_component(comp_node, registry))
        tg.tracks.append(track)

    effect.track_groups.append(tg)


def _decompile_component(node: LsxNode, registry: AllSparkRegistry) -> Component:
    """Convert a single runtime EffectComponent node into a toolkit Component."""
    class_name = node.attr_value("Type", node.attr_value("Name", ""))
    start = node.attr_value("StartTime", node.attr_value("Start", "0"))
    end = node.attr_value("EndTime", node.attr_value("End", "0"))
    # Deterministic fallback: derive from class/start/end so repeated
    # decompilations produce the same ID for the same component.
    fallback_id = str(uuid.uuid5(_NS_LSFX, f"{class_name}:{start}:{end}"))
    comp = Component(
        class_name=class_name,
        start=start,
        end=end,
        instance_name=node.attr_value("ID", fallback_id),
    )

    # Find the Properties container child
    props_container = None
    for child in node.children:
        if child.id == "Properties":
            props_container = child
            break

    if props_container is None:
        return comp

    for prop_node in props_container.children:
        if prop_node.id != "Property":
            continue
        _decompile_property(prop_node, comp, registry)

    return comp


def _decompile_property(prop_node: LsxNode, comp: Component,
                        registry: AllSparkRegistry) -> None:
    """Convert a runtime Property node into a toolkit Property on the Component."""
    full_name = prop_node.attr_value("FullName", "")
    attr_name = prop_node.attr_value("AttributeName", full_name)
    prop_type = _int_attr(prop_node, "Type", "7")

    # Resolve the AllSpark GUID for this property
    prop_guid = registry.resolve_best_name_to_guid(comp.class_name, full_name, attr_name)
    if prop_guid is None:
        _output.warnings.warn(f"No GUID for property '{full_name}' on '{comp.class_name}'")
        prop_guid = full_name or attr_name

    if prop_type in (PROP_TYPE_KEYFRAMED, PROP_TYPE_COLOR):
        _decompile_keyframed_property(prop_node, comp, prop_guid, prop_type)
    elif prop_type in (PROP_TYPE_RANGE, PROP_TYPE_INT32_RANGE):
        _decompile_range_property(prop_node, comp, prop_guid)
    else:
        _decompile_simple_property(prop_node, comp, prop_guid, prop_type)


def _decompile_simple_property(prop_node: LsxNode, comp: Component,
                               prop_guid: str, prop_type: int) -> None:
    """Handle a simple-value property (bool, float, string, vector3)."""
    raw_value = prop_node.attr_value("Value", "")

    if prop_type == PROP_TYPE_BOOL:
        toolkit_value = "1" if raw_value in ("True", "true", "1") else "0"
    elif prop_type == PROP_TYPE_VECTOR3:
        toolkit_value = ",".join(raw_value.split())
    else:
        toolkit_value = raw_value

    datum = Datum(value=toolkit_value)
    comp.properties.append(Property(guid=prop_guid, data=[datum]))


def _decompile_range_property(prop_node: LsxNode, comp: Component,
                              prop_guid: str) -> None:
    """Handle a range property (Type=5) with Min/Max attributes."""
    min_val = prop_node.attr_value("Min", "0")
    max_val = prop_node.attr_value("Max", "0")
    datum = Datum(value=f"{min_val},{max_val}")
    comp.properties.append(Property(guid=prop_guid, data=[datum]))


def _decompile_keyframed_property(prop_node: LsxNode, comp: Component,
                                  prop_guid: str, prop_type: int = PROP_TYPE_KEYFRAMED) -> None:
    """Handle a keyframed property (Type=6 or Type=3) with Frames children."""
    is_color = prop_type == PROP_TYPE_COLOR
    frames_nodes = prop_node.children_with_id("Frames")
    if not frames_nodes:
        raw = prop_node.attr_value("Value", "0")
        if is_color:
            raw = _fvec4_to_packed_color(raw) if " " in raw else raw
        datum = Datum(value=raw)
        comp.properties.append(Property(guid=prop_guid, data=[datum]))
        return

    channels: list[RampChannel] = []
    for chan_idx, frames_node in enumerate(frames_nodes):
        frame_type = _int_attr(frames_node, "FrameType")
        is_spline = frame_type == FRAME_TYPE_SPLINE

        channel_type = "Spline" if is_spline else "Linear"
        channel_id = str(uuid.uuid5(_NS_LSFX, f"{prop_guid}:ch{chan_idx}"))

        keyframes: list[Keyframe] = []
        for frame_node in frames_node.children_with_id("Frame"):
            time_val = frame_node.attr_value("Time", "0")

            # Color frames (both Linear and Spline) use Color:fvec4
            color_val = frame_node.attr_value("Color", "")
            if color_val:
                value_val = _fvec4_to_packed_color(color_val)
            elif is_spline:
                # Scalar Spline: Cubic Hermite f(t) = At³ + Bt² + Ct + D
                value_val = frame_node.attr_value("D", "0")
            else:
                # Linear scalar frame
                value_val = frame_node.attr_value("Value", "0")

            keyframes.append(Keyframe(time=time_val, value=value_val))

        channels.append(RampChannel(
            channel_type=channel_type,
            id=channel_id,
            selected=len(channels) == 0,
            keyframes=keyframes,
        ))

    rcd = RampChannelData(channels=channels)
    datum = Datum(ramp_channel_data=rcd)
    comp.properties.append(Property(guid=prop_guid, data=[datum]))


# ═══════════════════════════════════════════════════════════════════
#  EffectResource  →  LsxResource  (compilation / .lsefx → .lsfx)
# ═══════════════════════════════════════════════════════════════════

def effect_to_lsx(effect: EffectResource, registry: AllSparkRegistry) -> LsxResource:
    """Convert a toolkit EffectResource to a runtime LsxResource (for .lsfx binary)."""
    component_nodes: list[LsxNode] = []

    track_index = 0
    for tg in effect.track_groups:
        for track in tg.tracks:
            # Skip muted tracks — the binary compiler strips them
            if track.muted == "True":
                track_index += 1
                continue
            for comp in track.components:
                node = _compile_component(comp, track_index, registry)
                component_nodes.append(node)
            track_index += 1

    effect_components = LsxNode(id="EffectComponents", children=component_nodes)
    effect_node = LsxNode(
        id="Effect",
        attributes=[
            LsxNodeAttribute(id="Duration", attr_type="float",
                             value=_compute_duration(effect)),
        ],
        children=[effect_components],
    )
    effect_region = LsxRegion(id="Effect", nodes=[effect_node])
    return LsxResource(regions=[effect_region])


def _compute_duration(effect: EffectResource) -> str:
    """Compute the effect duration from the max EndTime of all components."""
    max_end = 0.0
    for tg in effect.track_groups:
        for track in tg.tracks:
            for comp in track.components:
                try:
                    max_end = max(max_end, float(comp.end))
                except ValueError:
                    pass
    return str(max_end)


def _compile_component(comp: Component, track_index: int,
                       registry: AllSparkRegistry) -> LsxNode:
    """Convert a toolkit Component into a runtime EffectComponent node."""
    attrs: list[LsxNodeAttribute] = [
        LsxNodeAttribute(id="EndTime", attr_type="float", value=comp.end),
        LsxNodeAttribute(id="ID", attr_type="guid", value=comp.instance_name),
        LsxNodeAttribute(id="Name", attr_type="LSString", value=comp.class_name),
        LsxNodeAttribute(id="StartTime", attr_type="float", value=comp.start),
        LsxNodeAttribute(id="Track", attr_type="uint32", value=str(track_index)),
        LsxNodeAttribute(id="Type", attr_type="LSString", value=comp.class_name),
    ]

    property_nodes: list[LsxNode] = []
    for prop in comp.properties:
        if prop.guid.lower() in _EDITOR_ONLY_PROPERTY_GUIDS:
            continue

        prop_name = registry.resolve_best_guid_to_name(comp.class_name, prop.guid)
        if prop_name is None:
            _output.warnings.warn(f"Unknown property GUID '{prop.guid}' -- using GUID as name")
            prop_name = prop.guid

        attr_name = _get_attribute_name(prop_name)
        is_color = _is_color_property(prop.guid, comp.class_name, registry)

        for datum in prop.data:
            if datum.ramp_channel_data is not None:
                prop_node = _compile_keyframed_property(
                    datum.ramp_channel_data, prop_name, attr_name,
                    comp.class_name, registry, is_color=is_color)
            elif datum.value is not None:
                prop_node = _compile_simple_property(
                    datum.value, prop_name, attr_name,
                    comp.class_name, registry)
            else:
                continue
            property_nodes.append(prop_node)

    children: list[LsxNode] = []
    if property_nodes:
        children.append(LsxNode(id="Properties", children=property_nodes))

    return LsxNode(id="EffectComponent", attributes=attrs, children=children)


def _get_attribute_name(full_name: str) -> str:
    """Derive the short AttributeName from the FullName.

    E.g. "Particle.Appearance.Brightness" → "Brightness"
    """
    if "." in full_name:
        return full_name.rsplit(".", 1)[-1]
    return full_name


def _is_color_property(prop_guid: str, component_class: str,
                       registry: AllSparkRegistry) -> bool:
    """Check if a property is a Color type (ColorRamp) via AllSpark."""
    comp_def = registry.components.get(component_class)
    if comp_def:
        pdef = comp_def.properties.get(prop_guid.lower())
        if pdef and pdef.type_name:
            return pdef.type_name in ("ColorRamp", "Color", "Vector4")
    return False


def _infer_runtime_prop_type(value: str, full_name: str,
                             component_class: str,
                             registry: AllSparkRegistry) -> int:
    """Infer the runtime Property Type enum from AllSpark metadata and value."""
    comp_def = registry.components.get(component_class)
    if comp_def:
        # Try FullName-based resolution first
        guid = registry.resolve_full_name_to_guid(component_class, full_name)
        if guid is None:
            guid = registry.resolve_property_name(component_class, full_name)
        if guid:
            prop_def = comp_def.properties.get(guid.lower())
            if prop_def and prop_def.type_name:
                return _allspark_type_to_prop_type(prop_def.type_name)

    return _guess_prop_type(value)


def _allspark_type_to_prop_type(allspark_type: str) -> int:
    """Map AllSpark type name to runtime Property Type enum."""
    return _ALLSPARK_TYPE_MAP.get(allspark_type, PROP_TYPE_STRING)


def _guess_prop_type(value: str) -> int:
    """Best-effort type inference from a toolkit datum value string."""
    if value.lower() in ("true", "false"):
        return PROP_TYPE_BOOL
    if "," in value:
        parts = value.split(",")
        if len(parts) == 2:
            try:
                float(parts[0])
                float(parts[1])
                return PROP_TYPE_RANGE
            except ValueError:
                pass
        if len(parts) == 3:
            return PROP_TYPE_VECTOR3
        if len(parts) == 4:
            return PROP_TYPE_COLOR
    try:
        float(value)
        return PROP_TYPE_FLOAT
    except ValueError:
        pass
    return PROP_TYPE_STRING


def _compile_simple_property(value: str, full_name: str, attr_name: str,
                             component_class: str,
                             registry: AllSparkRegistry) -> LsxNode:
    """Build a runtime Property node for a simple (non-keyframed) value."""
    prop_type = _infer_runtime_prop_type(value, full_name, component_class, registry)

    attrs: list[LsxNodeAttribute] = [
        LsxNodeAttribute(id="AttributeName", attr_type="FixedString", value=attr_name),
        LsxNodeAttribute(id="FullName", attr_type="FixedString", value=full_name),
        LsxNodeAttribute(id="Type", attr_type="uint8", value=str(prop_type)),
    ]

    if prop_type == PROP_TYPE_RANGE:
        parts = value.split(",", 1)
        attrs.append(LsxNodeAttribute(id="Min", attr_type="float", value=parts[0] if parts else "0"))
        attrs.append(LsxNodeAttribute(id="Max", attr_type="float", value=parts[1] if len(parts) > 1 else "0"))
    elif prop_type == PROP_TYPE_INT32_RANGE:
        parts = value.split(",", 1)
        attrs.append(LsxNodeAttribute(id="Min", attr_type="int32", value=parts[0] if parts else "0"))
        attrs.append(LsxNodeAttribute(id="Max", attr_type="int32", value=parts[1] if len(parts) > 1 else "0"))
    elif prop_type == PROP_TYPE_BOOL:
        runtime_val = "True" if value in ("1", "true", "True") else "False"
        attrs.append(LsxNodeAttribute(id="Value", attr_type="bool", value=runtime_val))
    elif prop_type == PROP_TYPE_INT32:
        attrs.append(LsxNodeAttribute(id="Value", attr_type="int32", value=value))
    elif prop_type == PROP_TYPE_COLOR:
        parts = value.split(",")
        while len(parts) < 4:
            parts.append("0")
        runtime_val = " ".join(parts[:4])
        attrs.append(LsxNodeAttribute(id="Value", attr_type="fvec4", value=runtime_val))
    elif prop_type == PROP_TYPE_VECTOR3:
        parts = value.split(",")
        while len(parts) < 3:
            parts.append("0")
        runtime_val = " ".join(parts[:3])
        attrs.append(LsxNodeAttribute(id="Value", attr_type="fvec3", value=runtime_val))
    elif prop_type == PROP_TYPE_FLOAT:
        attrs.append(LsxNodeAttribute(id="Value", attr_type="float", value=value))
    elif prop_type == PROP_TYPE_RESOURCE:
        runtime_val = _strip_resource_display_name(value)
        attrs.append(LsxNodeAttribute(id="Value", attr_type="FixedString", value=runtime_val))
    else:
        attrs.append(LsxNodeAttribute(id="Value", attr_type="LSString", value=value))

    return LsxNode(id="Property", attributes=attrs)


def _compile_keyframed_property(rcd: RampChannelData, full_name: str,
                                attr_name: str, component_class: str,
                                registry: AllSparkRegistry,
                                is_color: bool = False) -> LsxNode:
    """Build a runtime Property node with Frames children for keyframed data."""
    prop_type = PROP_TYPE_COLOR if is_color else PROP_TYPE_KEYFRAMED
    attrs: list[LsxNodeAttribute] = [
        LsxNodeAttribute(id="AttributeName", attr_type="FixedString", value=attr_name),
        LsxNodeAttribute(id="FullName", attr_type="FixedString", value=full_name),
        LsxNodeAttribute(id="Type", attr_type="uint8", value=str(prop_type)),
    ]

    frames_children: list[LsxNode] = []
    for channel in rcd.channels:
        ct = channel.channel_type.lower()
        is_spline = ct in ("spline", "freetangentspline")
        frame_type = FRAME_TYPE_SPLINE if is_spline else FRAME_TYPE_LINEAR

        frame_nodes: list[LsxNode] = []
        for kf in channel.keyframes:
            if is_color:
                # Color frames: packed int → RGBA fvec4
                color_val = _packed_color_to_fvec4(kf.value)
                frame_attrs = [
                    LsxNodeAttribute(id="Color", attr_type="fvec4", value=color_val),
                    LsxNodeAttribute(id="Time", attr_type="float", value=kf.time),
                ]
            elif is_spline:
                frame_attrs = [
                    LsxNodeAttribute(id="A", attr_type="float", value="0"),
                    LsxNodeAttribute(id="B", attr_type="float", value="0"),
                    LsxNodeAttribute(id="C", attr_type="float", value="0"),
                    LsxNodeAttribute(id="D", attr_type="float", value=kf.value),
                    LsxNodeAttribute(id="Time", attr_type="float", value=kf.time),
                ]
            else:
                frame_attrs = [
                    LsxNodeAttribute(id="Time", attr_type="float", value=kf.time),
                    LsxNodeAttribute(id="Value", attr_type="float", value=kf.value),
                ]
            frame_nodes.append(LsxNode(id="Frame", attributes=frame_attrs))

        frames_node = LsxNode(
            id="Frames",
            attributes=[
                LsxNodeAttribute(id="FrameType", attr_type="uint8", value=str(frame_type)),
            ],
            children=frame_nodes,
        )
        frames_children.append(frames_node)

    return LsxNode(id="Property", attributes=attrs, children=frames_children)
