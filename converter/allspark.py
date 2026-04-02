"""Parser for AllSpark definition files (ComponentDefinition.xcd / ModuleDefinition.xmd).

These files ship with the BG3 Toolkit and provide the authoritative GUID → name
mapping for every property and module type used in .lsefx effect files.
"""

# Security note: xml.etree.ElementTree is safe here — Python 3.8+ disables
# external entity expansion by default (expat does not support it), and input
# files are local AllSpark .xcd/.xmd definitions, not untrusted network data.
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from . import _output
from .errors import RegistryError


@dataclass
class PropertyDef:
    name: str
    guid: str                       # lowercase
    type_name: str = ""             # e.g. "FloatSlider", "Vector3", "Boolean"
    specializable: bool = False
    tooltip: str = ""
    default_value: str = ""


@dataclass
class ComponentDef:
    name: str
    tooltip: str = ""
    color: str = ""
    properties: dict[str, PropertyDef] = field(default_factory=dict)  # guid → PropDef


@dataclass
class ModuleDef:
    name: str
    guid: str                       # lowercase
    properties: dict[str, str] = field(default_factory=dict)  # guid → name


class AllSparkRegistry:
    """Holds the merged property registry from XCD + XMD files.

    Usage::

        reg = AllSparkRegistry()
        reg.load("ComponentDefinition.xcd", "ModuleDefinition.xmd")
        # Resolve a GUID to a human-readable name
        name = reg.guid_to_name.get(some_guid)  # e.g. "Lifetime"
        # Resolve a dotted FullName within a component
        fn = reg.guid_to_full_name.get("ParticleSystem", {}).get(some_guid)

    **FullName semantics:** Property groups in the XCD form a dotted path.
    For example, a property "Speed" nested under group "Emission" in
    component "ParticleSystem" has FullName ``"Emission.Speed"``. These
    FullName paths match the ``FullName`` attribute in LSF binary files and
    are used during transform to align properties between formats.

    **Engine version note:** The XCD/XMD files are version-specific; the
    property set changed between BG3 Patch 5–7. Always use the definition
    files that match your game installation.
    """

    def __init__(self) -> None:
        self.components: dict[str, ComponentDef] = {}      # component_name → def
        self.guid_to_name: dict[str, str] = {}             # guid (lower) → property name
        self.name_to_guid: dict[str, dict[str, str]] = {}  # component → {name → guid}
        self._global_name_to_guid: dict[str, str] = {}     # flat name → guid (all components)
        self.modules: dict[str, ModuleDef] = {}            # module_name → def
        self.module_guid_to_name: dict[str, str] = {}      # guid (lower) → module name
        self.module_name_to_guid: dict[str, str] = {}      # module name → guid
        # FullName mappings derived from propertygroup hierarchy
        self.guid_to_full_name: dict[str, dict[str, str]] = {}  # comp → {guid → dotted_path}
        self.full_name_to_guid: dict[str, dict[str, str]] = {}  # comp → {dotted_path → guid}
        # Required-module properties — scoped per component type via the
        # component="..." attribute in the XMD.  {(component, name) → guid}
        self._required_comp_name_to_guid: dict[tuple[str, str], str] = {}
        # Reverse mapping: property GUID → module GUID (for module reconstruction)
        self._prop_guid_to_module_guid: dict[str, str] = {}
        # Phase definition GUIDs from XCD (positional: Lead In, Loop, Lead Out)
        self.phase_definition_ids: list[str] = []
        # Resolution caches (populated lazily)
        self._name_to_guid_cache: dict[tuple, str | None] = {}
        self._guid_to_name_cache: dict[tuple, str | None] = {}

    def load_xcd(self, path: str | Path) -> None:
        """Parse a ComponentDefinition.xcd file."""
        try:
            tree = ET.parse(path)
        except FileNotFoundError:
            raise FileNotFoundError(f"AllSpark XCD file not found: {path}") from None
        except ET.ParseError as e:
            raise RegistryError(f"Malformed XCD file {path}: {e}") from e
        root = tree.getroot()

        for comp_el in root.iter("component"):
            comp_name = comp_el.get("name", "")
            comp_def = ComponentDef(
                name=comp_name,
                tooltip=comp_el.get("tooltip", ""),
                color=comp_el.get("color", ""),
            )

            for prop_el in comp_el.iter("property"):
                name = prop_el.get("name", "")
                guid = (prop_el.get("id") or "").lower()
                if not guid:
                    continue
                # Skip bare property refs inside <propertygroup> (no name, no definition)
                if not name:
                    continue

                defn_el = prop_el.find("definition")
                type_name = ""
                tooltip = ""
                default_val = ""
                specializable = False

                if defn_el is not None:
                    type_name = defn_el.get("type", "")
                    tooltip = defn_el.get("tooltip", "")
                    specializable = defn_el.get("specializable", "").lower() == "true"
                    # Try to read default datum value
                    data_el = defn_el.find("data")
                    if data_el is not None:
                        datum_el = data_el.find("datum")
                        if datum_el is not None:
                            default_val = datum_el.get("value", "")

                prop_def = PropertyDef(
                    name=name, guid=guid, type_name=type_name,
                    specializable=specializable, tooltip=tooltip,
                    default_value=default_val,
                )
                comp_def.properties[guid] = prop_def
                self.guid_to_name[guid] = name

                # Build reverse map
                if comp_name not in self.name_to_guid:
                    self.name_to_guid[comp_name] = {}
                self.name_to_guid[comp_name][name] = guid
                self._global_name_to_guid.setdefault(name, guid)

            self.components[comp_name] = comp_def

            # Parse propertygroup hierarchy for FullName dotted paths
            self._parse_propertygroup_hierarchy(comp_el, comp_name)

        # Parse PhaseDefinition objects from the <phases> section
        phases_el = root.find("phases")
        if phases_el is not None:
            for obj_el in phases_el.findall("object"):
                data_el = obj_el.find("data")
                if data_el is not None:
                    phase_id = (data_el.get("id") or "").lower()
                    if phase_id:
                        self.phase_definition_ids.append(phase_id)

    def _parse_propertygroup_hierarchy(self, comp_el: ET.Element, comp_name: str) -> None:
        """Parse the <propertygroup> hierarchy within a component to build FullName dotted paths.

        FullName for a property in the binary is: group1.group2.PropertyName.
        The root "Property Group" is stripped from the path.
        """
        guid_map: dict[str, str] = {}   # guid → dotted_full_name
        name_map: dict[str, str] = {}   # dotted_full_name → guid

        # Find the root propertygroup(s) — can be direct children or inside <properties>
        for pg_el in comp_el.findall("propertygroup"):
            self._walk_propertygroup(pg_el, "", comp_name, guid_map, name_map, is_root=True)
        for props_el in comp_el.findall("properties"):
            for pg_el in props_el.findall("propertygroup"):
                self._walk_propertygroup(pg_el, "", comp_name, guid_map, name_map, is_root=True)

        if guid_map:
            self.guid_to_full_name[comp_name] = guid_map
            self.full_name_to_guid[comp_name] = name_map

    def _walk_propertygroup(self, pg_el: ET.Element, prefix: str,
                            comp_name: str, guid_map: dict[str, str],
                            name_map: dict[str, str], is_root: bool = False) -> None:
        """Recursively walk a propertygroup element to build FullName paths."""
        group_name = pg_el.get("name", "")

        # Build the current prefix
        if is_root:
            current_prefix = ""  # Root group doesn't contribute to the path
        elif prefix:
            current_prefix = f"{prefix}.{group_name}"
        else:
            current_prefix = group_name

        # Properties directly under this group
        for props_el in pg_el.findall("properties"):
            for prop_el in props_el.findall("property"):
                guid = (prop_el.get("id") or "").lower()
                if not guid:
                    continue
                # Look up the property name from the flat definitions
                prop_name = self.guid_to_name.get(guid, "")
                if not prop_name:
                    continue

                if current_prefix:
                    full_name = f"{current_prefix}.{prop_name}"
                else:
                    full_name = prop_name

                guid_map[guid] = full_name
                name_map[full_name] = guid

        # Recurse into child propertygroups
        for children_el in pg_el.findall("children"):
            for child_pg in children_el.findall("propertygroup"):
                self._walk_propertygroup(child_pg, current_prefix, comp_name,
                                         guid_map, name_map)

    def load_xmd(self, path: str | Path) -> None:
        """Parse a ModuleDefinition.xmd file."""
        try:
            tree = ET.parse(path)
        except FileNotFoundError:
            raise FileNotFoundError(f"AllSpark XMD file not found: {path}") from None
        except ET.ParseError as e:
            raise RegistryError(f"Malformed XMD file {path}: {e}") from e
        root = tree.getroot()

        for mod_el in root.iter("module"):
            name = mod_el.get("name", "")
            guid = (mod_el.get("id") or "").lower()
            if not guid:
                continue

            mod_def = ModuleDef(name=name, guid=guid)

            # Modules define per-component property bindings
            for prop_el in mod_el.iter("property"):
                prop_name = prop_el.get("name", "")
                prop_guid = (prop_el.get("id") or "").lower()
                if prop_guid:
                    mod_def.properties[prop_guid] = prop_name
                    # These module-defined properties also participate in the
                    # global GUID → name mapping
                    self.guid_to_name[prop_guid] = prop_name
                    # Track which module each property belongs to
                    self._prop_guid_to_module_guid[prop_guid] = guid
                    # "Required" module properties are scoped per component
                    if name == "Required":
                        prop_comp = prop_el.get("component", "")
                        if prop_comp:
                            self._required_comp_name_to_guid[(prop_comp, prop_name)] = prop_guid

            self.modules[name] = mod_def
            self.module_guid_to_name[guid] = name
            self.module_name_to_guid[name] = guid

    def load(self, xcd_path: str | Path, xmd_path: str | Path) -> None:
        """Convenience: load both definition files."""
        self.load_xcd(xcd_path)
        if len(self.guid_to_name) < 100:
            _output.warnings.warn(
                f"XCD loaded only {len(self.guid_to_name)} properties "
                f"(expected ≥100) — check that the file is complete"
            )
        self.load_xmd(xmd_path)

    def resolve_property_guid(self, guid: str) -> str | None:
        """Look up a property GUID (case-insensitive) and return its name."""
        return self.guid_to_name.get(guid.lower())

    def resolve_property_full_name(self, component_class: str, guid: str) -> str | None:
        """Look up a property GUID and return its dotted FullName for the given component.

        Returns the full dotted path (e.g. "Dynamic Parameters.Color") if the
        propertygroup hierarchy was parsed, otherwise falls back to the short name.
        """
        guid_lower = guid.lower()
        comp_map = self.guid_to_full_name.get(component_class)
        if comp_map:
            full = comp_map.get(guid_lower)
            if full:
                return full
        # Fall back to short name
        return self.guid_to_name.get(guid_lower)

    def resolve_full_name_to_guid(self, component_class: str, full_name: str) -> str | None:
        """Look up a dotted FullName within a component and return its property GUID."""
        comp_map = self.full_name_to_guid.get(component_class)
        if comp_map:
            found = comp_map.get(full_name)
            if found:
                return found
        # Fall back to regular name resolution
        return self.resolve_property_name(component_class, full_name)

    def resolve_property_name(self, component_class: str, name: str) -> str | None:
        """Look up a property name within a component class and return its GUID."""
        comp_map = self.name_to_guid.get(component_class)
        if comp_map:
            found = comp_map.get(name)
            if found:
                return found
        # Module Required properties are scoped per component type
        required = self._required_comp_name_to_guid.get((component_class, name))
        if required:
            return required
        # Fall back to global flat dict (module properties are not component-scoped)
        return self._global_name_to_guid.get(name)

    def resolve_module_guid(self, guid: str) -> str | None:
        """Look up a module GUID and return its name."""
        return self.module_guid_to_name.get(guid.lower())

    def resolve_module_name(self, name: str) -> str | None:
        """Look up a module name and return its GUID."""
        return self.module_name_to_guid.get(name)

    def resolve_best_name_to_guid(self, component_class: str,
                                  full_name: str, attr_name: str) -> str | None:
        """Resolve a property name to GUID using the full cascade with caching."""
        key = (component_class, full_name, attr_name)
        if key in self._name_to_guid_cache:
            return self._name_to_guid_cache[key]

        # 1. Dotted FullNames (e.g. "Custom.Float 01.Name") resolve via the
        #    component's propertygroup hierarchy.
        comp_fn_map = self.full_name_to_guid.get(component_class)
        guid = comp_fn_map.get(full_name) if comp_fn_map else None
        if guid is None and full_name != attr_name and comp_fn_map:
            guid = comp_fn_map.get(attr_name)

        # 2. Bare names (no dots) that match a Required-module property
        #    resolve to the component-scoped Required GUID — these are
        #    properties like "Name" and "Visible" that every component
        #    inherits, but with distinct GUIDs per component type.
        if guid is None and "." not in full_name:
            guid = self._required_comp_name_to_guid.get((component_class, full_name))
        if guid is None and "." not in attr_name and full_name != attr_name:
            guid = self._required_comp_name_to_guid.get((component_class, attr_name))

        # 3. Component-scoped name → GUID, then global fallback.
        if guid is None:
            guid = self.resolve_property_name(component_class, full_name)
        if guid is None and full_name != attr_name:
            guid = self.resolve_property_name(component_class, attr_name)
        if guid is None and "." in full_name:
            short_name = full_name.rsplit(".", 1)[-1]
            guid = self.resolve_property_name(component_class, short_name)

        self._name_to_guid_cache[key] = guid
        return guid

    def resolve_best_guid_to_name(self, component_class: str, guid: str) -> str | None:
        """Resolve a property GUID to its best name (full dotted path preferred), with caching."""
        key = (component_class, guid)
        if key in self._guid_to_name_cache:
            return self._guid_to_name_cache[key]

        name = self.resolve_property_full_name(component_class, guid)
        if name is None:
            name = self.resolve_property_guid(guid)

        self._guid_to_name_cache[key] = name
        return name

    def resolve_component_modules(self, component_class: str,
                                  property_guids: list[str]) -> list[str]:
        """Return ordered list of module GUIDs for a component based on its properties.

        The Required module is always first.  Non-Required modules are added in
        the order their properties are first encountered in *property_guids*.
        """
        required_guid = self.module_name_to_guid.get("Required", "").lower()

        seen: set[str] = set()
        result: list[str] = []

        if required_guid:
            result.append(required_guid)
            seen.add(required_guid)

        for pg in property_guids:
            mod_guid = self._prop_guid_to_module_guid.get(pg.lower())
            if mod_guid and mod_guid not in seen:
                result.append(mod_guid)
                seen.add(mod_guid)

        return result
