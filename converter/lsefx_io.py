"""Reader/writer for .lsefx toolkit XML files."""

# Security note: xml.etree.ElementTree is safe here — Python 3.8+ disables
# external entity expansion by default (expat does not support it), and input
# files are local .lsefx files, not untrusted network data.
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import IO

from .effect_model import (
    NIL_UUID,
    Component,
    Datum,
    EffectResource,
    Keyframe,
    Module,
    PlatformMetadata,
    Property,
    PropertyGroup,
    RampChannel,
    RampChannelData,
    Track,
    TrackGroup,
    TrackGroupId,
)


# ── Reader ──────────────────────────────────────────────────────────

def read_lsefx(source: str | Path | IO[bytes]) -> EffectResource:
    """Parse an .lsefx XML file into an EffectResource."""
    try:
        tree = ET.parse(source)
    except FileNotFoundError:
        raise FileNotFoundError(f"LSEFX file not found: {source}") from None
    except ET.ParseError as e:
        raise ValueError(f"Malformed .lsefx XML in {source}: {e}") from e
    root = tree.getroot()

    if root.tag != "effect":
        raise ValueError(f"Expected <effect> root element, got <{root.tag}>")

    effect = EffectResource(
        version=root.get("version", "0.0"),
        effect_version=root.get("effectversion", "1.0.0"),
        id=root.get("id", NIL_UUID),
    )

    # phases / colors — preserve as opaque XML snippets for round-trip fidelity
    phases_el = root.find("phases")
    if phases_el is not None:
        effect.phases = list(phases_el)  # child Elements

    colors_el = root.find("colors")
    if colors_el is not None:
        effect.colors = list(colors_el)

    tgs_el = root.find("trackgroups")
    if tgs_el is not None:
        for tg_el in tgs_el.findall("trackgroup"):
            effect.track_groups.append(_parse_trackgroup(tg_el))

    return effect


def _parse_trackgroup(el: ET.Element) -> TrackGroup:
    tg = TrackGroup(name=el.get("name", ""))

    ids_el = el.find("ids")
    if ids_el is not None:
        for id_el in ids_el.findall("id"):
            tg.ids.append(TrackGroupId(value=id_el.get("value", "")))

    for track_el in el.findall("track"):
        tg.tracks.append(_parse_track(track_el))
    return tg


def _parse_track(el: ET.Element) -> Track:
    track = Track(
        name=el.get("name", "Track"),
        muted=el.get("muted", "False"),
        locked=el.get("locked", "False"),
        mute_state_override=el.get("mutestateoverride", "None"),
    )
    for comp_el in el.findall("component"):
        track.components.append(_parse_component(comp_el))
    return track


def _parse_component(el: ET.Element) -> Component:
    comp = Component(
        class_name=el.get("class", ""),
        start=el.get("start", "0"),
        end=el.get("end", "0"),
        instance_name=el.get("instancename", ""),
    )

    props_el = el.find("properties")
    if props_el is not None:
        for child in props_el:
            if child.tag == "property":
                comp.properties.append(_parse_property(child))
            elif child.tag == "propertygroup":
                comp.property_groups.append(PropertyGroup(
                    guid=child.get("id", ""),
                    name=child.get("name", ""),
                    collapsed=child.get("collapsed", "False"),
                ))

    mods_el = el.find("modules")
    if mods_el is not None:
        for mod_el in mods_el.findall("module"):
            raw_index = mod_el.get("index", "0")
            try:
                index = int(raw_index)
            except ValueError:
                raise ValueError(
                    f"Non-integer module index {raw_index!r} on <module id={mod_el.get('id', '?')!r}>"
                )
            comp.modules.append(Module(
                guid=mod_el.get("id", ""),
                muted=mod_el.get("muted", "False"),
                index=index,
            ))
    return comp


def _parse_property(el: ET.Element) -> Property:
    prop = Property(guid=el.get("id", ""))

    data_el = el.find("data")
    if data_el is not None:
        for datum_el in data_el.findall("datum"):
            prop.data.append(_parse_datum(datum_el))

    for pm_el in el.findall("platformmetadata"):
        prop.platform_metadata.append(PlatformMetadata(
            platform=pm_el.get("platform", ""),
            expanded=pm_el.get("expanded", "True"),
        ))
    return prop


def _parse_datum(el: ET.Element) -> Datum:
    rcd = None
    rcd_el = el.find("rampchanneldata")
    if rcd_el is not None:
        rcd = _parse_rampchanneldata(rcd_el)

    return Datum(
        platform=el.get("platform", NIL_UUID),
        lod=el.get("lod", NIL_UUID),
        value=el.get("value"),  # None if not present (rampchanneldata instead)
        ramp_channel_data=rcd,
    )


def _parse_rampchanneldata(el: ET.Element) -> RampChannelData:
    rcd = RampChannelData()
    for rc_el in el.findall("rampchannel"):
        channel = RampChannel(
            channel_type=rc_el.get("type", "Linear"),
            id=rc_el.get("id", ""),
            selected=rc_el.get("selected", "False").lower() == "true",
        )
        kfs_el = rc_el.find("keyframes")
        if kfs_el is not None:
            for kf_el in kfs_el.findall("keyframe"):
                channel.keyframes.append(Keyframe(
                    time=kf_el.get("time", "0"),
                    value=kf_el.get("value", "0"),
                    interpolation=kf_el.get("interpolation"),
                ))
        rcd.channels.append(channel)
    return rcd


# ── Writer ──────────────────────────────────────────────────────────

def write_lsefx(effect: EffectResource, dest: str | Path | IO[bytes]) -> None:
    """Write an EffectResource to .lsefx XML."""
    root = ET.Element("effect")
    root.set("version", effect.version)
    root.set("effectversion", effect.effect_version)
    root.set("id", effect.id)

    # phases
    phases_el = ET.SubElement(root, "phases")
    for child in effect.phases:
        phases_el.append(child)

    # colors
    colors_el = ET.SubElement(root, "colors")
    for child in effect.colors:
        colors_el.append(child)

    # trackgroups
    tgs_el = ET.SubElement(root, "trackgroups")
    for tg in effect.track_groups:
        _write_trackgroup(tgs_el, tg)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if isinstance(dest, (str, Path)):
        tree.write(str(dest), encoding="utf-8", xml_declaration=True)
    else:
        tree.write(dest, encoding="utf-8", xml_declaration=True)


def _write_trackgroup(parent: ET.Element, tg: TrackGroup) -> None:
    el = ET.SubElement(parent, "trackgroup")
    el.set("name", tg.name)

    ids_el = ET.SubElement(el, "ids")
    for tid in tg.ids:
        id_el = ET.SubElement(ids_el, "id")
        id_el.set("value", tid.value)

    for track in tg.tracks:
        _write_track(el, track)


def _write_track(parent: ET.Element, track: Track) -> None:
    el = ET.SubElement(parent, "track")
    el.set("name", track.name)
    el.set("muted", track.muted)
    el.set("locked", track.locked)
    el.set("mutestateoverride", track.mute_state_override)

    for comp in track.components:
        _write_component(el, comp)


def _write_component(parent: ET.Element, comp: Component) -> None:
    el = ET.SubElement(parent, "component")
    el.set("class", comp.class_name)
    el.set("start", comp.start)
    el.set("end", comp.end)
    el.set("instancename", comp.instance_name)

    props_el = ET.SubElement(el, "properties")
    for prop in comp.properties:
        _write_property(props_el, prop)
    for pg in comp.property_groups:
        pg_el = ET.SubElement(props_el, "propertygroup")
        pg_el.set("id", pg.guid)
        pg_el.set("name", pg.name)
        pg_el.set("collapsed", pg.collapsed)

    mods_el = ET.SubElement(el, "modules")
    for mod in comp.modules:
        mod_el = ET.SubElement(mods_el, "module")
        mod_el.set("id", mod.guid)
        mod_el.set("muted", mod.muted)
        mod_el.set("index", str(mod.index))


def _write_property(parent: ET.Element, prop: Property) -> None:
    el = ET.SubElement(parent, "property")
    el.set("id", prop.guid)

    data_el = ET.SubElement(el, "data")
    for datum in prop.data:
        _write_datum(data_el, datum)

    for pm in prop.platform_metadata:
        pm_el = ET.SubElement(el, "platformmetadata")
        pm_el.set("platform", pm.platform)
        pm_el.set("expanded", pm.expanded)


def _write_datum(parent: ET.Element, datum: Datum) -> None:
    el = ET.SubElement(parent, "datum")
    el.set("platform", datum.platform)
    el.set("lod", datum.lod)

    if datum.ramp_channel_data is not None:
        _write_rampchanneldata(el, datum.ramp_channel_data)
    elif datum.value is not None:
        el.set("value", datum.value)


def _write_rampchanneldata(parent: ET.Element, rcd: RampChannelData) -> None:
    rcd_el = ET.SubElement(parent, "rampchanneldata")
    for ch in rcd.channels:
        ch_el = ET.SubElement(rcd_el, "rampchannel")
        ch_el.set("type", ch.channel_type)
        ch_el.set("id", ch.id)
        ch_el.set("selected", "True" if ch.selected else "False")

        kfs_el = ET.SubElement(ch_el, "keyframes")
        for kf in ch.keyframes:
            kf_el = ET.SubElement(kfs_el, "keyframe")
            kf_el.set("time", kf.time)
            kf_el.set("value", kf.value)
            if kf.interpolation is not None:
                kf_el.set("interpolation", kf.interpolation)
