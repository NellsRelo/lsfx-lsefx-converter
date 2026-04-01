"""Toolkit effect data model — represents the domain-specific .lsefx structure."""

import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NIL_UUID = "00000000-0000-0000-0000-000000000000"


@dataclass
class Keyframe:
    time: str
    value: str
    interpolation: str | None = None


@dataclass
class RampChannel:
    channel_type: str          # "Linear", "Spline", etc.
    id: str                    # UUID
    selected: bool = False
    keyframes: list[Keyframe] = field(default_factory=list)


@dataclass
class RampChannelData:
    channels: list[RampChannel] = field(default_factory=list)


@dataclass
class PlatformMetadata:
    platform: str
    expanded: str = "True"


@dataclass
class Datum:
    platform: str = NIL_UUID
    lod: str = NIL_UUID
    value: str | None = None
    ramp_channel_data: RampChannelData | None = None


@dataclass
class Property:
    guid: str
    data: list[Datum] = field(default_factory=list)
    platform_metadata: list[PlatformMetadata] = field(default_factory=list)


@dataclass
class PropertyGroup:
    guid: str
    name: str
    collapsed: str = "False"


@dataclass
class Module:
    guid: str
    muted: str = "False"
    index: int = 0


@dataclass
class Component:
    class_name: str
    start: str = "0"
    end: str = "0"
    instance_name: str = ""
    properties: list[Property] = field(default_factory=list)
    property_groups: list[PropertyGroup] = field(default_factory=list)
    modules: list[Module] = field(default_factory=list)


@dataclass
class Track:
    name: str = "Track"
    muted: str = "False"
    locked: str = "False"
    mute_state_override: str = "Unmuted"
    components: list[Component] = field(default_factory=list)


@dataclass
class TrackGroupId:
    value: str


@dataclass
class TrackGroup:
    name: str = "New Track Group"
    ids: list[TrackGroupId] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)


@dataclass
class EffectResource:
    version: str = "0.0"
    effect_version: str = "1.0.0"
    id: str = NIL_UUID
    phases: list[ET.Element] = field(default_factory=list)
    colors: list[ET.Element] = field(default_factory=list)
    track_groups: list[TrackGroup] = field(default_factory=list)
