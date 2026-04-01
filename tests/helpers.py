"""Shared test helpers — extracted from duplicate code across debug/test scripts."""

import glob
import os

from converter.lsx_model import LsxNode, LsxResource

# Re-export path constants from conftest so callers can do:
#   from tests.helpers import LSFX_DIR, find_pairs
from tests.conftest import LSEFX_DIR, LSFX_DIR, XCD_PATH, XMD_PATH

# ── Float comparison ────────────────────────────────────────────────

FLOAT_TOL = 1e-6


def values_match(binary_val: str, compiled_val: str) -> bool:
    """Compare two values, treating float format differences as equal."""
    if binary_val == compiled_val:
        return True
    try:
        if abs(float(binary_val) - float(compiled_val)) < FLOAT_TOL:
            return True
    except (ValueError, TypeError):
        pass
    bp = binary_val.split()
    cp = compiled_val.split()
    if len(bp) == len(cp) and len(bp) > 1:
        try:
            return all(abs(float(a) - float(b)) < FLOAT_TOL for a, b in zip(bp, cp))
        except ValueError:
            pass
    bp2 = binary_val.replace(",", " ").split()
    cp2 = compiled_val.replace(",", " ").split()
    if len(bp2) == len(cp2) and len(bp2) > 1:
        try:
            return all(abs(float(a) - float(b)) < FLOAT_TOL for a, b in zip(bp2, cp2))
        except ValueError:
            pass
    return False


# ── LsxResource helpers ────────────────────────────────────────────

def get_effect_components(resource: LsxResource) -> list[LsxNode]:
    """Extract EffectComponent nodes from an LsxResource."""
    effect_reg = resource.region("Effect")
    if not effect_reg:
        return []
    for node in effect_reg.nodes:
        if node.id == "Effect":
            for child in node.children:
                if child.id == "EffectComponents":
                    return child.children
    return []


def get_properties(comp_node: LsxNode) -> dict[str, LsxNode]:
    """Extract Property nodes as dict of FullName -> node."""
    props: dict[str, LsxNode] = {}
    for child in comp_node.children:
        if child.id == "Properties":
            for pnode in child.children:
                if pnode.id == "Property":
                    fname = pnode.attr_value("FullName", "")
                    props[fname] = pnode
    return props


# ── File pair discovery ─────────────────────────────────────────────

def find_pairs(
    lsfx_dir: str = LSFX_DIR,
    lsefx_dir: str = LSEFX_DIR,
) -> list[tuple[str, str, str]]:
    """Find matching .lsfx / .lsefx files by base name.

    Returns list of (name, lsfx_path, lsefx_path) tuples.
    """
    lsfx_files: dict[str, str] = {}
    for f in glob.glob(os.path.join(lsfx_dir, "**", "*.lsfx"), recursive=True):
        name = os.path.splitext(os.path.basename(f))[0]
        lsfx_files[name] = f

    lsefx_files: dict[str, str] = {}
    for f in glob.glob(os.path.join(lsefx_dir, "**", "*.lsefx"), recursive=True):
        name = os.path.splitext(os.path.basename(f))[0]
        lsefx_files[name] = f

    paired = []
    for name in sorted(set(lsfx_files.keys()) & set(lsefx_files.keys())):
        paired.append((name, lsfx_files[name], lsefx_files[name]))

    return paired
