"""Integration tests comparing vanilla .lsfx decompiles against vanilla .lsefx files.

Usage:
    python -m tests.test_parity [--limit N] [--verbose]
"""

import argparse
import glob
import io
import os
import sys
import warnings
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from converter.allspark import AllSparkRegistry
from converter.lsf_reader import read_lsf
from converter.lsefx_io import read_lsefx, write_lsefx
from converter.transform import lsx_to_effect

from tests.conftest import LSFX_DIR, LSEFX_DIR, XCD_PATH, XMD_PATH


@dataclass
class FileResult:
    name: str
    success: bool = False
    error: str = ""
    our_components: int = 0
    van_components: int = 0
    our_properties: int = 0
    van_properties: int = 0
    warn_count: int = 0
    matched_props: int = 0
    missing_props: int = 0
    extra_props: int = 0
    comp_class_match: bool = True
    details: list[str] = field(default_factory=list)


def count_components(effect):
    return sum(1 for tg in effect.track_groups for t in tg.tracks for c in t.components)


def count_properties(effect):
    return sum(len(c.properties) for tg in effect.track_groups for t in tg.tracks for c in t.components)


def get_components(effect):
    return [c for tg in effect.track_groups for t in tg.tracks for c in t.components]


def compare_effects(our, vanilla) -> FileResult:
    """Deep comparison between our decompiled effect and the vanilla .lsefx."""
    result = FileResult(name="")

    our_comps = get_components(our)
    van_comps = get_components(vanilla)
    result.our_components = len(our_comps)
    result.van_components = len(van_comps)

    # Compare component class names (order may differ)
    our_classes = sorted(c.class_name for c in our_comps)
    van_classes = sorted(c.class_name for c in van_comps)
    if our_classes != van_classes:
        result.comp_class_match = False
        # What's different?
        our_set = {}
        van_set = {}
        for cls in our_classes:
            our_set[cls] = our_set.get(cls, 0) + 1
        for cls in van_classes:
            van_set[cls] = van_set.get(cls, 0) + 1
        for cls in set(our_set.keys()) | set(van_set.keys()):
            oc = our_set.get(cls, 0)
            vc = van_set.get(cls, 0)
            if oc != vc:
                result.details.append(f"  class {cls}: ours={oc} vanilla={vc}")

    # Property-level comparison: build a map by component class + property guid
    our_prop_map = {}
    for c in our_comps:
        for p in c.properties:
            key = (c.class_name, c.instance_name, p.guid)
            our_prop_map[key] = p

    van_prop_map = {}
    for c in van_comps:
        for p in c.properties:
            key = (c.class_name, c.instance_name, p.guid)
            van_prop_map[key] = p

    # Keys comparison
    our_keys = set(our_prop_map.keys())
    van_keys = set(van_prop_map.keys())

    # Since instance_name (GUID) won't match between decompile and vanilla,
    # use class_name + property guid only, counted
    our_by_class = {}
    van_by_class = {}
    for c in our_comps:
        for p in c.properties:
            k = (c.class_name, p.guid)
            our_by_class[k] = our_by_class.get(k, 0) + 1

    for c in van_comps:
        for p in c.properties:
            k = (c.class_name, p.guid)
            van_by_class[k] = van_by_class.get(k, 0) + 1

    all_keys = set(our_by_class.keys()) | set(van_by_class.keys())
    result.matched_props = 0
    result.missing_props = 0
    result.extra_props = 0
    for k in all_keys:
        oc = our_by_class.get(k, 0)
        vc = van_by_class.get(k, 0)
        matched = min(oc, vc)
        result.matched_props += matched
        if vc > oc:
            result.missing_props += vc - oc
        if oc > vc:
            result.extra_props += oc - vc

    result.our_properties = sum(our_by_class.values())
    result.van_properties = sum(van_by_class.values())

    result.success = (result.our_components == result.van_components
                      and result.comp_class_match
                      and result.missing_props == 0
                      and result.extra_props == 0)

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
    only_lsfx = []
    only_lsefx = []
    for name in sorted(set(lsfx_files.keys()) | set(lsefx_files.keys())):
        if name in lsfx_files and name in lsefx_files:
            paired.append((name, lsfx_files[name], lsefx_files[name]))
        elif name in lsfx_files:
            only_lsfx.append(name)
        else:
            only_lsefx.append(name)

    return paired, only_lsfx, only_lsefx


def run_parity_test(limit=None, verbose=False):
    print("Loading AllSpark registry...")
    registry = AllSparkRegistry()
    registry.load(XCD_PATH, XMD_PATH)
    print(f"  {len(registry.guid_to_name)} property GUIDs, {len(registry.components)} components")

    print("Finding file pairs...")
    paired, only_lsfx, only_lsefx = find_pairs()
    print(f"  {len(paired)} pairs, {len(only_lsfx)} lsfx-only, {len(only_lsefx)} lsefx-only")

    if limit:
        paired = paired[:limit]
        print(f"  Testing first {limit} pairs")

    results: list[FileResult] = []
    errors = 0
    perfect = 0
    total_our_comps = 0
    total_van_comps = 0
    total_our_props = 0
    total_van_props = 0
    total_matched = 0
    total_missing = 0
    total_extra = 0
    total_warns = 0

    for i, (name, lsfx_path, lsefx_path) in enumerate(paired):
        try:
            # Read the binary .lsfx
            lsx_res = read_lsf(lsfx_path)

            # Decompile
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                our_effect = lsx_to_effect(lsx_res, registry)
                warn_count = len(w)

            # Read vanilla .lsefx
            vanilla_effect = read_lsefx(lsefx_path)

            # Compare
            result = compare_effects(our_effect, vanilla_effect)
            result.name = name
            result.warn_count = warn_count
            result.success = result.success  # already set

            total_our_comps += result.our_components
            total_van_comps += result.van_components
            total_our_props += result.our_properties
            total_van_props += result.van_properties
            total_matched += result.matched_props
            total_missing += result.missing_props
            total_extra += result.extra_props
            total_warns += warn_count

            if result.success and warn_count == 0:
                perfect += 1

            if verbose and not result.success:
                print(f"\n  [{i+1}] {name}: MISMATCH")
                print(f"    Components: {result.our_components} vs {result.van_components}")
                print(f"    Properties: matched={result.matched_props} missing={result.missing_props} extra={result.extra_props}")
                if result.details:
                    for d in result.details[:5]:
                        print(f"    {d}")

        except Exception as e:
            result = FileResult(name=name, error=str(e))
            errors += 1
            if verbose:
                print(f"\n  [{i+1}] {name}: ERROR — {e}")

        results.append(result)

        if (i + 1) % 50 == 0:
            print(f"  ... {i+1}/{len(paired)} processed")

    # ── Summary ──────────────────────────────────────────────
    n = len(paired)
    print(f"\n{'='*60}")
    print(f"PARITY REPORT — {n} files tested")
    print(f"{'='*60}")
    print(f"Perfect matches:  {perfect}/{n} ({100*perfect/n:.1f}%)")
    print(f"Errors (crashes):  {errors}")
    print()
    print(f"Components:  ours={total_our_comps}  vanilla={total_van_comps}  delta={total_our_comps - total_van_comps}")
    print(f"Properties:  ours={total_our_props}  vanilla={total_van_props}")
    print(f"  Matched:   {total_matched}")
    print(f"  Missing:   {total_missing} (in vanilla but not ours)")
    print(f"  Extra:     {total_extra} (in ours but not vanilla)")
    print(f"Warnings:    {total_warns}")

    if total_van_props > 0:
        print(f"\nProperty parity: {100*total_matched/total_van_props:.1f}%")

    # Show most common class mismatches
    class_delta = {}
    for r in results:
        if not r.comp_class_match:
            for d in r.details:
                class_delta[d.strip()] = class_delta.get(d.strip(), 0) + 1

    if class_delta:
        print(f"\nMost common class mismatches:")
        for d, cnt in sorted(class_delta.items(), key=lambda x: -x[1])[:15]:
            print(f"  ({cnt}x) {d}")

    # Show files with most missing props
    worst = sorted([r for r in results if r.missing_props > 0],
                   key=lambda r: -r.missing_props)
    if worst:
        print(f"\nFiles with most missing properties:")
        for r in worst[:10]:
            print(f"  {r.name}: missing={r.missing_props} (ours={r.our_properties} vanilla={r.van_properties})")

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="Max number of pairs to test")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    run_parity_test(limit=args.limit, verbose=args.verbose)
