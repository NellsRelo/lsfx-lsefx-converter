"""Compile-direction parity test: vanilla .lsefx → compile → compare with vanilla .lsfx.

Tests the critical path for modding: users edit .lsefx and compile to .lsfx.
Compares at the LsxResource level (structural property match).

Usage:
    python -m tests.test_compile_parity [--limit N] [--verbose]
"""

import argparse
import glob
import os
import sys
import warnings
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from converter.allspark import AllSparkRegistry
from converter.lsf_reader import read_lsf
from converter.lsefx_io import read_lsefx
from converter.transform import effect_to_lsx

from tests.conftest import LSFX_DIR, LSEFX_DIR, XCD_PATH, XMD_PATH


@dataclass
class CompileResult:
    name: str
    success: bool = False
    error: str = ""
    compiled_comps: int = 0
    binary_comps: int = 0
    total_matched: int = 0
    total_missing: int = 0
    total_extra: int = 0
    type_diffs: int = 0
    value_diffs: int = 0
    warn_count: int = 0
    details: list[str] = field(default_factory=list)


def get_effect_components(resource):
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


def get_properties(comp_node):
    """Extract Property nodes as dict of FullName -> node."""
    props = {}
    for child in comp_node.children:
        if child.id == "Properties":
            for pnode in child.children:
                if pnode.id == "Property":
                    fname = pnode.attr_value("FullName", "")
                    props[fname] = pnode
    return props


# float32 can drift ~1e-7 from the "clean" decimal when round-tripped
_FLOAT_TOL = 1e-6


def values_match(binary_val: str, compiled_val: str) -> bool:
    """Compare two values, treating float format differences as equal."""
    if binary_val == compiled_val:
        return True
    # Try float normalization (scalar)
    try:
        if abs(float(binary_val) - float(compiled_val)) < _FLOAT_TOL:
            return True
    except (ValueError, TypeError):
        pass
    # Try space-separated vector normalization
    bp = binary_val.split()
    cp = compiled_val.split()
    if len(bp) == len(cp) and len(bp) > 1:
        try:
            return all(abs(float(a) - float(b)) < _FLOAT_TOL for a, b in zip(bp, cp))
        except ValueError:
            pass
    # Try comma-separated (toolkit) vs space-separated (binary)
    bp2 = binary_val.replace(",", " ").split()
    cp2 = compiled_val.replace(",", " ").split()
    if len(bp2) == len(cp2) and len(bp2) > 1:
        try:
            return all(abs(float(a) - float(b)) < _FLOAT_TOL for a, b in zip(bp2, cp2))
        except ValueError:
            pass
    return False


def compare_compiled(compiled, binary, verbose=False):
    """Compare compiled LsxResource with vanilla binary at property level."""
    result = CompileResult(name="")

    compiled_comps = get_effect_components(compiled)
    binary_comps = get_effect_components(binary)
    result.compiled_comps = len(compiled_comps)
    result.binary_comps = len(binary_comps)

    compiled_by_id = {c.attr_value("ID", ""): c for c in compiled_comps}
    binary_by_id = {c.attr_value("ID", ""): c for c in binary_comps}

    for bid, bcomp in binary_by_id.items():
        ccomp = compiled_by_id.get(bid)
        btype = bcomp.attr_value("Type", "?")

        if not ccomp:
            result.total_missing += 1
            result.details.append(f"MISSING component {btype} ({bid[:16]}...)")
            continue

        bprops = get_properties(bcomp)
        cprops = get_properties(ccomp)

        matched = set(bprops.keys()) & set(cprops.keys())
        missing = set(bprops.keys()) - set(cprops.keys())
        extra = set(cprops.keys()) - set(bprops.keys())

        result.total_matched += len(matched)
        result.total_missing += len(missing)
        result.total_extra += len(extra)

        for fname in matched:
            bp = bprops[fname]
            cp = cprops[fname]
            bt = bp.attr_value("Type", "")
            ct = cp.attr_value("Type", "")
            if bt != ct:
                result.type_diffs += 1

            # Compare values (float-aware)
            bv = bp.attr_value("Value", "")
            cv = cp.attr_value("Value", "")
            if bv or cv:
                if not values_match(bv, cv):
                    result.value_diffs += 1
                    if verbose and result.value_diffs <= 3:
                        result.details.append(f"  VALUE: {fname}: {bv!r} vs {cv!r}")
            # Compare Min/Max for range properties
            bmin = bp.attr_value("Min", "")
            cmin = cp.attr_value("Min", "")
            bmax = bp.attr_value("Max", "")
            cmax = cp.attr_value("Max", "")
            if (bmin or cmin) and not values_match(bmin, cmin):
                result.value_diffs += 1
                if verbose and result.value_diffs <= 3:
                    result.details.append(f"  MIN: {fname}: {bmin!r} vs {cmin!r}")
            if (bmax or cmax) and not values_match(bmax, cmax):
                result.value_diffs += 1
                if verbose and result.value_diffs <= 3:
                    result.details.append(f"  MAX: {fname}: {bmax!r} vs {cmax!r}")

        if missing and verbose:
            for m in sorted(missing)[:3]:
                result.details.append(f"  MISSING: {m}")
        if extra and verbose:
            for e in sorted(extra)[:3]:
                result.details.append(f"  EXTRA: {e}")

    # Extra compiled components not in binary
    extra_ids = set(compiled_by_id.keys()) - set(binary_by_id.keys())
    if extra_ids:
        result.total_extra += len(extra_ids)
        for eid in extra_ids:
            ctype = compiled_by_id[eid].attr_value("Type", "?")
            result.details.append(f"EXTRA component {ctype} ({eid[:16]}...)")

    result.success = (result.total_missing == 0 and result.total_extra == 0
                      and result.type_diffs == 0)

    return result


def find_pairs():
    """Find matching .lsfx / .lsefx files by base name."""
    lsfx_files = {}
    for f in glob.glob(os.path.join(LSFX_DIR, "**", "*.lsfx"), recursive=True):
        name = os.path.splitext(os.path.basename(f))[0]
        lsfx_files[name] = f

    lsefx_files = {}
    for f in glob.glob(os.path.join(LSEFX_DIR, "**", "*.lsefx"), recursive=True):
        name = os.path.splitext(os.path.basename(f))[0]
        lsefx_files[name] = f

    paired = []
    for name in sorted(set(lsfx_files.keys()) & set(lsefx_files.keys())):
        paired.append((name, lsfx_files[name], lsefx_files[name]))

    return paired


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    print("Loading AllSpark registry...")
    registry = AllSparkRegistry()
    registry.load(XCD_PATH, XMD_PATH)
    print(f"  {len(registry.guid_to_name)} properties, "
          f"{len(registry.guid_to_full_name)} components with FullName maps")

    print("Finding file pairs...")
    paired = find_pairs()
    print(f"  {len(paired)} pairs")

    if args.limit:
        paired = paired[:args.limit]
        print(f"  Testing first {args.limit}")

    results: list[CompileResult] = []
    perfect = 0
    struct_perfect = 0
    errors = 0
    totals = {"matched": 0, "missing": 0, "extra": 0, "type_diffs": 0, "value_diffs": 0}

    for i, (name, lsfx_path, lsefx_path) in enumerate(paired):
        try:
            # Read vanilla .lsefx and compile
            effect = read_lsefx(lsefx_path)
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                compiled = effect_to_lsx(effect, registry)
                n_warns = len(w)

            # Read vanilla binary
            binary = read_lsf(lsfx_path)

            # Compare
            result = compare_compiled(compiled, binary, verbose=args.verbose)
            result.name = name
            result.warn_count = n_warns

            totals["matched"] += result.total_matched
            totals["missing"] += result.total_missing
            totals["extra"] += result.total_extra
            totals["type_diffs"] += result.type_diffs
            totals["value_diffs"] += result.value_diffs

            if result.success and result.value_diffs == 0:
                perfect += 1
            if result.success:
                struct_perfect += 1

            if args.verbose and (not result.success or result.value_diffs > 0):
                status = "MISMATCH" if not result.success else "VALUE_DIFFS"
                print(f"\n  [{i+1}] {name}: {status} "
                      f"(comps {result.compiled_comps}/{result.binary_comps}, "
                      f"missing={result.total_missing} extra={result.total_extra} "
                      f"type={result.type_diffs} values={result.value_diffs})")
                for d in result.details[:8]:
                    print(f"    {d}")

        except Exception as e:
            result = CompileResult(name=name, error=str(e))
            errors += 1
            if args.verbose:
                print(f"\n  [{i+1}] {name}: ERROR — {e}")

        results.append(result)
        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(paired)} processed")

    n = len(paired)
    print(f"\n{'='*60}")
    print(f"COMPILE PARITY REPORT — {n} files tested")
    print(f"{'='*60}")
    print(f"Structural match (0 missing, 0 extra, 0 type diffs): {struct_perfect}/{n} ({100*struct_perfect/n:.1f}%)")
    print(f"Perfect match (incl. values): {perfect}/{n} ({100*perfect/n:.1f}%)")
    print(f"Crashes: {errors}")
    print()
    print(f"Properties matched:  {totals['matched']}")
    print(f"Properties missing:  {totals['missing']}")
    print(f"Properties extra:    {totals['extra']}")
    print(f"Type mismatches:     {totals['type_diffs']}")
    print(f"Value differences:   {totals['value_diffs']}")

    if totals["matched"] + totals["missing"] > 0:
        pct = 100 * totals["matched"] / (totals["matched"] + totals["missing"])
        print(f"\nProperty match rate: {pct:.2f}%")

    # Show files with most issues
    worst = sorted([r for r in results if r.total_missing > 0],
                   key=lambda r: -r.total_missing)
    if worst:
        print(f"\nFiles with most missing properties:")
        for r in worst[:10]:
            print(f"  {r.name}: missing={r.total_missing} extra={r.total_extra}")

    errs = [r for r in results if r.error]
    if errs:
        print(f"\nFiles that crashed:")
        for r in errs[:10]:
            print(f"  {r.name}: {r.error[:80]}")


if __name__ == "__main__":
    main()
