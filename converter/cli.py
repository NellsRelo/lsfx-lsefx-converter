"""Command-line interface for the LSFX ↔ LSEFX converter."""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from . import _output
from ._output import Verbosity
from .allspark import AllSparkRegistry
from .effect_model import EffectResource
from .errors import ConverterError
from .lsefx_io import read_lsefx, write_lsefx
from .lsf_reader import read_lsf
from .lsf_writer import write_lsf
from .lsx_model import LsxResource
from .transform import effect_to_lsx, lsx_to_effect


# ── Color helpers (UX-010) ──────────────────────────────────────────

def _supports_color() -> bool:
    """Return True if stderr is a TTY that likely supports ANSI colors."""
    return hasattr(sys.stderr, "isatty") and sys.stderr.isatty()

_COLOR_ENABLED = False

def _init_color(no_color: bool) -> None:
    global _COLOR_ENABLED
    _COLOR_ENABLED = not no_color and _supports_color()

def _color(text: str, code: str) -> str:
    if _COLOR_ENABLED:
        return f"\033[{code}m{text}\033[0m"
    return text

def _green(text: str) -> str:
    return _color(text, "32")

def _red(text: str) -> str:
    return _color(text, "31")

def _yellow(text: str) -> str:
    return _color(text, "33")

# Relative path from BG3 game root to AllSpark config files
_ALLSPARK_REL = Path("Data") / "Editor" / "Config" / "AllSpark"
_XCD_NAME = "ComponentDefinition.xcd"
_XMD_NAME = "ModuleDefinition.xmd"

# Extension → opposite command hint
_EXT_HINTS: dict[str, str] = {
    ".lsfx": "decompile",
    ".lsefx": "compile",
}


def _add_registry_args(parser: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive --game / (--xcd + --xmd) arguments."""
    group = parser.add_argument_group("AllSpark registry (pick one)")
    mx = group.add_mutually_exclusive_group()
    mx.add_argument(
        "--game",
        help='BG3 game root directory, e.g. "C:\\SteamLibrary\\...\\Baldurs Gate 3"',
    )
    mx.add_argument("--xcd", help="Path to ComponentDefinition.xcd (requires --xmd)")
    group.add_argument("--xmd", help="Path to ModuleDefinition.xmd (requires --xcd)")


def _resolve_registry_args(args: argparse.Namespace) -> None:
    """Resolve --game into --xcd/--xmd, or validate that both were given."""
    if args.game:
        game = Path(args.game)
        args.xcd = str(game / _ALLSPARK_REL / _XCD_NAME)
        args.xmd = str(game / _ALLSPARK_REL / _XMD_NAME)
    elif bool(args.xcd) != bool(args.xmd):
        _output.error("Error: Must provide both --xcd and --xmd together.")
        sys.exit(1)
    elif args.xcd and args.xmd:
        pass  # explicit paths provided
    else:
        _output.error("Error: Provide either --game or both --xcd and --xmd.")
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lsfx-converter",
        description="Two-way converter between BG3 .lsfx (binary) and .lsefx (toolkit XML) effect files.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.1.0")

    # Global flags
    verbosity = parser.add_mutually_exclusive_group()
    verbosity.add_argument(
        "-v", "--verbose", action="store_true",
        help="Show verbose diagnostics (AllSpark resolution details, etc.)",
    )
    verbosity.add_argument(
        "-q", "--quiet", action="store_true",
        help="Suppress informational output; only show errors and results",
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable colored output even when connected to a TTY",
    )
    parser.add_argument(
        "--time", action="store_true",
        help="Print elapsed time for the operation",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit machine-readable JSON output (for dump and registry commands)",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # ── decompile: .lsfx → .lsefx ──
    dec = subparsers.add_parser(
        "decompile",
        help="Convert runtime .lsfx binary(s) to toolkit .lsefx XML file(s).",
        epilog="Examples:\n"
               "  %(prog)s --game \"C:\\BG3\" input.lsfx\n"
               "  %(prog)s --game \"C:\\BG3\" Effects/ -o output/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dec.add_argument("input", help="Path to an .lsfx file or a folder (recursive)")
    dec.add_argument("-o", "--output", help="Output path (file or folder; default: alongside input)")
    dec.add_argument("-f", "--force", action="store_true", help="Overwrite existing output files")
    dec.add_argument("-n", "--dry-run", action="store_true", help="List files that would be processed, without converting")
    _add_registry_args(dec)

    # ── compile: .lsefx → .lsfx ──
    comp = subparsers.add_parser(
        "compile",
        help="Convert toolkit .lsefx XML file(s) to runtime .lsfx binary(s).",
        epilog="Examples:\n"
               "  %(prog)s --game \"C:\\BG3\" input.lsefx\n"
               "  %(prog)s --game \"C:\\BG3\" Effects/ -o output/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    comp.add_argument("input", help="Path to an .lsefx file or a folder (recursive)")
    comp.add_argument("-o", "--output", help="Output path (file or folder; default: alongside input)")
    comp.add_argument("-f", "--force", action="store_true", help="Overwrite existing output files")
    comp.add_argument("-n", "--dry-run", action="store_true", help="List files that would be processed, without converting")
    _add_registry_args(comp)

    # ── roundtrip: verify conversion fidelity ──
    rt = subparsers.add_parser(
        "roundtrip",
        help="Convert .lsfx -> .lsefx -> .lsfx and report differences.",
        epilog="Examples:\n"
               "  %(prog)s --game \"C:\\BG3\" input.lsfx\n"
               "  %(prog)s --game \"C:\\BG3\" Effects/\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    rt.add_argument("input", help="Path to an .lsfx file or a folder (recursive)")
    _add_registry_args(rt)
    rt.add_argument("--keep", action="store_true", help="Keep intermediate files")

    # ── dump: show the LsxResource tree from a binary .lsfx ──
    dump = subparsers.add_parser(
        "dump",
        help="Parse .lsfx binary(s) and print the LsxResource tree (no AllSpark needed).",
        epilog="Examples:\n"
               "  %(prog)s input.lsfx\n"
               "  %(prog)s input.lsfx --max-depth 3 --full\n"
               "  %(prog)s Effects/ --json\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dump.add_argument("input", help="Path to an .lsfx file or a directory (recursive)")
    dump.add_argument("--max-depth", type=int, default=10, help="Max tree depth to display (default: %(default)s)")
    dump.add_argument("--full", action="store_true", help="Show full attribute values (no truncation)")

    # ── registry: inspect AllSpark definitions ──
    reg_cmd = subparsers.add_parser(
        "registry",
        help="Inspect the AllSpark registry (components, properties, modules).",
        epilog="Examples:\n"
               "  %(prog)s --game \"C:\\BG3\"\n"
               "  %(prog)s --game \"C:\\BG3\" --search Radius\n"
               "  %(prog)s --game \"C:\\BG3\" --component ParticleSystem\n"
               "  %(prog)s --game \"C:\\BG3\" --json\n",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    reg_cmd.add_argument("--search", metavar="TERM", help="Search for properties/components by name (case-insensitive)")
    reg_cmd.add_argument("--component", metavar="NAME", help="Show details for a specific component")
    _add_registry_args(reg_cmd)

    # Activate tab completion if argcomplete is installed (UX-015)
    try:
        import argcomplete
        argcomplete.autocomplete(parser)
    except ImportError:
        pass

    args = parser.parse_args(argv)

    # Apply verbosity
    if args.verbose:
        _output.set_verbosity(Verbosity.VERBOSE)
    elif args.quiet:
        _output.set_verbosity(Verbosity.QUIET)

    _init_color(args.no_color)
    t0 = time.perf_counter()

    if args.command == "dump":
        _cmd_dump(args)
        _print_timing(t0, args)
        return 0

    if args.command == "registry":
        _resolve_registry_args(args)
        _cmd_registry(args)
        _print_timing(t0, args)
        return 0

    _resolve_registry_args(args)

    if args.command == "decompile":
        _cmd_decompile(args)
    elif args.command == "compile":
        _cmd_compile(args)
    elif args.command == "roundtrip":
        _cmd_roundtrip(args)
    _print_timing(t0, args)
    return 0


def _print_timing(t0: float, args: argparse.Namespace) -> None:
    """Print elapsed time if --time flag is set (UX-011)."""
    if getattr(args, "time", False):
        elapsed = time.perf_counter() - t0
        _output.info(f"Elapsed: {elapsed:.3f}s")


def _load_registry(args: argparse.Namespace) -> AllSparkRegistry:
    reg = AllSparkRegistry()
    try:
        reg.load(args.xcd, args.xmd)
    except (FileNotFoundError, ValueError, ConverterError) as e:
        _output.error(f"Error loading AllSpark registry: {e}")
        if "not found" in str(e).lower():
            _output.error("  Check that --game points to the BG3 install directory, "
                          "or provide explicit --xcd and --xmd paths.")
        sys.exit(1)
    _output.info(f"Loaded AllSpark: {len(reg.guid_to_name)} property GUIDs, "
                 f"{len(reg.components)} components, {len(reg.modules)} modules")
    return reg


def _collect_files(input_path: Path, extension: str) -> list[Path]:
    """Return a list of files to process. If *input_path* is a directory,
    recursively find all files with *extension*; otherwise return it as-is."""
    if input_path.is_dir():
        files = sorted(p for p in input_path.rglob(f"*{extension}") if not p.is_symlink())
        if not files:
            # Check for files of the opposite format as a hint
            opposite = ".lsefx" if extension == ".lsfx" else ".lsfx"
            opposite_files = list(input_path.rglob(f"*{opposite}"))
            if opposite_files:
                hint_cmd = _EXT_HINTS.get(opposite, "")
                _output.error(f"No {extension} files found in {input_path}")
                _output.error(f"  (found {len(opposite_files)} {opposite} file(s) -- "
                              f"did you mean '{hint_cmd}'?)")
            else:
                _output.error(f"No {extension} files found in {input_path}")
            sys.exit(1)
        return files
    if not input_path.exists():
        _output.error(f"Error: File not found: {input_path}")
        sys.exit(1)
    # Single-file: validate extension
    if input_path.suffix.lower() != extension.lower():
        hint_cmd = _EXT_HINTS.get(input_path.suffix.lower(), "")
        msg = f"Error: Expected {extension} file, got '{input_path.suffix}'"
        if hint_cmd:
            msg += f" -- did you mean '{hint_cmd}'?"
        _output.error(msg)
        sys.exit(1)
    return [input_path]


def _resolve_output(input_file: Path, input_root: Path,
                    output_arg: str | None, new_ext: str) -> Path:
    """Compute the output path for *input_file*.

    * Single-file mode (input_root == input_file): use *output_arg* or swap
      the extension.
    * Bulk mode: mirror the input subdirectory structure under *output_arg*
      (or beside the input file if no output dir given).
    """
    if input_root == input_file:
        # Single-file
        if output_arg:
            out = Path(output_arg)
            return out / input_file.with_suffix(new_ext).name if out.is_dir() else out
        return input_file.with_suffix(new_ext)
    # Bulk — preserve relative structure
    rel = input_file.relative_to(input_root)
    if output_arg:
        base = Path(output_arg)
    else:
        base = input_root
    return base / rel.with_suffix(new_ext)


def _cmd_convert(
    args: argparse.Namespace,
    *,
    input_ext: str,
    output_ext: str,
    read_fn: Callable[[str], Any],
    transform_fn: Callable[[Any, AllSparkRegistry], Any],
    write_fn: Callable[[Any, str], None],
    print_summaries: Callable[[Any, Any], None],
) -> None:
    """Shared implementation for decompile and compile commands."""
    input_path = Path(args.input)
    files = _collect_files(input_path, input_ext)
    input_root = input_path if input_path.is_dir() else input_path
    dry_run = getattr(args, "dry_run", False)

    if dry_run:
        _output.info(f"Dry run: {len(files)} {input_ext} file(s) would be processed")
        for f in files:
            out = _resolve_output(f, input_root, args.output, output_ext)
            print(f"  {f} -> {out}")
        return

    reg = _load_registry(args)
    force = getattr(args, "force", False)

    if len(files) > 1:
        _output.info(f"Processing {len(files)} {input_ext} file(s)...")

    _output.warnings.reset()
    ok, fail = 0, 0
    for f in files:
        out = _resolve_output(f, input_root, args.output, output_ext)
        out_tmp = out.with_suffix(out.suffix + ".tmp")
        try:
            if not force and out.exists():
                _output.error(f"  [{ok + fail + 1}/{len(files)}] {_yellow('SKIPPED')} {f.name}: "
                              f"output exists (use --force to overwrite)")
                fail += 1
                continue
            input_data = read_fn(str(f))
            output_data = transform_fn(input_data, reg)
            out.parent.mkdir(parents=True, exist_ok=True)
            write_fn(output_data, str(out_tmp))
            out_tmp.replace(out)
            ok += 1
            if len(files) > 1:
                _output.info(f"  [{ok + fail}/{len(files)}] {f.name}")
            else:
                print_summaries(input_data, output_data)
                print(f"Written to {out}")
        except Exception as e:
            fail += 1
            _output.error(f"  [{ok + fail}/{len(files)}] {_red('FAILED')} {f.name}: {e}")
        finally:
            if out_tmp.exists():
                out_tmp.unlink()

    if len(files) > 1:
        warn_str = f", {_output.warnings.count} warning(s)" if _output.warnings.count else ""
        _output.info(f"\nDone: {_green(str(ok))} succeeded, {_red(str(fail))} failed out of {len(files)}{warn_str}")
    elif _output.warnings.count:
        _output.info(f"  ({_output.warnings.count} warning(s))")
    if fail > 0:
        sys.exit(2)


def _cmd_decompile(args: argparse.Namespace) -> None:
    _cmd_convert(
        args,
        input_ext=".lsfx",
        output_ext=".lsefx",
        read_fn=read_lsf,
        transform_fn=lsx_to_effect,
        write_fn=write_lsefx,
        print_summaries=lambda i, o: (_print_resource_summary(i), _print_effect_summary(o)),
    )


def _cmd_compile(args: argparse.Namespace) -> None:
    _cmd_convert(
        args,
        input_ext=".lsefx",
        output_ext=".lsfx",
        read_fn=read_lsefx,
        transform_fn=effect_to_lsx,
        write_fn=write_lsf,
        print_summaries=lambda i, o: (_print_effect_summary(i), _print_resource_summary(o)),
    )


def _cmd_roundtrip(args: argparse.Namespace) -> None:
    reg = _load_registry(args)
    input_path = Path(args.input)
    files = _collect_files(input_path, ".lsfx")

    if len(files) > 1:
        _output.info(f"Roundtripping {len(files)} .lsfx file(s)...")

    _output.warnings.reset()
    ok, fail = 0, 0
    for f in files:
        try:
            if len(files) > 1:
                _output.info(f"\n── {f.name} ──")

            original = read_lsf(str(f))
            _print_resource_summary(original)

            effect = lsx_to_effect(original, reg)
            _print_effect_summary(effect)

            if args.keep:
                intermediate = f.with_suffix(".roundtrip.lsefx")
                write_lsefx(effect, str(intermediate))
                _output.info(f"  Intermediate written to {intermediate}")

            rebuilt = effect_to_lsx(effect, reg)
            _compare_resources(original, rebuilt)

            if args.keep:
                output = f.with_suffix(".roundtrip.lsfx")
                write_lsf(rebuilt, str(output))
                _output.info(f"  Rebuilt written to {output}")

            ok += 1
        except Exception as e:
            fail += 1
            if args.keep:
                for suffix in (".roundtrip.lsefx", ".roundtrip.lsfx"):
                    stale = f.with_suffix(suffix)
                    if stale.exists():
                        stale.unlink()
            _output.error(f"  [{ok + fail}/{len(files)}] {_red('FAILED')} {f.name}: {e}")
    if len(files) > 1:
        warn_str = f", {_output.warnings.count} warning(s)" if _output.warnings.count else ""
        _output.info(f"\nDone: {ok} succeeded, {fail} failed out of {len(files)}{warn_str}")
    if fail > 0:
        sys.exit(2)


def _cmd_dump(args: argparse.Namespace) -> None:
    input_path = Path(args.input)
    if not input_path.exists():
        _output.error(f"Error: File not found: {input_path}")
        sys.exit(1)

    # Support directory input (UX-008)
    if input_path.is_dir():
        files = sorted(p for p in input_path.rglob("*.lsfx") if not p.is_symlink())
        if not files:
            _output.error(f"No .lsfx files found in {input_path}")
            sys.exit(1)
    else:
        files = [input_path]

    use_json = getattr(args, "json", False)
    json_results: list[dict] = []

    for fpath in files:
        try:
            if not use_json and len(files) > 1:
                print(f"\n{'=' * 60}")
                print(f"  {fpath}")
                print(f"{'=' * 60}")

            resource = read_lsf(str(fpath))
        except (ValueError, ConverterError, OSError) as e:
            if use_json:
                json_results.append({"file": str(fpath), "error": str(e)})
            else:
                _output.error(f"Error reading {fpath}: {e}")
            continue

        if use_json:
            json_results.append({"file": str(fpath), "tree": _resource_to_dict(resource)})
        else:
            if len(files) == 1:
                _output.info(f"Reading {fpath} ...")
            _print_resource_summary(resource)
            full = getattr(args, "full", False)
            for region in resource.regions:
                print(f"\nRegion: {region.id}")
                for node in region.nodes:
                    _print_node(node, indent=1, max_depth=args.max_depth, full=full)

    if use_json:
        if len(files) == 1 and len(json_results) == 1:
            print(json.dumps(json_results[0], indent=2))
        else:
            print(json.dumps(json_results, indent=2))


def _resource_to_dict(resource: LsxResource) -> dict:
    """Convert an LsxResource tree to a JSON-serializable dict (UX-018)."""
    return {
        "regions": [
            {
                "id": r.id,
                "nodes": [_node_to_dict(n) for n in r.nodes],
            }
            for r in resource.regions
        ]
    }


def _node_to_dict(node) -> dict:
    d: dict[str, Any] = {"id": node.id}
    if node.key_attribute:
        d["key_attribute"] = node.key_attribute
    if node.attributes:
        d["attributes"] = [
            {"id": a.id, "type": a.attr_type, "value": a.value}
            for a in node.attributes
        ]
    if node.children:
        d["children"] = [_node_to_dict(c) for c in node.children]
    return d


def _cmd_registry(args: argparse.Namespace) -> None:
    """Inspect AllSpark registry: list components, search properties (UX-024)."""
    reg = _load_registry(args)
    use_json = getattr(args, "json", False)
    search_term = getattr(args, "search", None)
    component = getattr(args, "component", None)

    if component:
        comp_def = reg.components.get(component)
        if not comp_def:
            _output.error(f"Component '{component}' not found in registry.")
            _output.error(f"  Available: {', '.join(sorted(reg.components)[:20])}")
            if len(reg.components) > 20:
                _output.error(f"  ... and {len(reg.components) - 20} more")
            sys.exit(1)
        if use_json:
            print(json.dumps(_component_to_dict(comp_def, reg, component), indent=2))
        else:
            _print_component_detail(comp_def, reg, component)
        return

    if search_term:
        term = search_term.lower()
        matches_comp = [n for n in reg.components if term in n.lower()]
        matches_prop = [(g, n) for g, n in reg.guid_to_name.items() if term in n.lower()]

        if use_json:
            print(json.dumps({
                "components": matches_comp,
                "properties": [{"guid": g, "name": n} for g, n in matches_prop],
            }, indent=2))
        else:
            if matches_comp:
                print(f"Components matching '{search_term}' ({len(matches_comp)}):")
                for name in sorted(matches_comp):
                    print(f"  {name}")
            if matches_prop:
                print(f"\nProperties matching '{search_term}' ({len(matches_prop)}):")
                for guid, name in sorted(matches_prop, key=lambda x: x[1]):
                    print(f"  {name}  ({guid})")
            if not matches_comp and not matches_prop:
                _output.info(f"No matches for '{search_term}'.")
        return

    # Default: summary
    if use_json:
        print(json.dumps({
            "components": len(reg.components),
            "properties": len(reg.guid_to_name),
            "modules": len(reg.modules),
            "component_names": sorted(reg.components),
            "module_names": sorted(reg.modules),
        }, indent=2))
    else:
        print(f"AllSpark Registry Summary:")
        print(f"  Components: {len(reg.components)}")
        print(f"  Properties: {len(reg.guid_to_name)} GUIDs")
        print(f"  Modules:    {len(reg.modules)}")
        print(f"\nComponents:")
        for name in sorted(reg.components):
            comp = reg.components[name]
            print(f"  {name} ({len(comp.properties)} properties)")
        if reg.modules:
            print(f"\nModules:")
            for name in sorted(reg.modules):
                print(f"  {name}  ({reg.modules[name].guid})")


def _component_to_dict(comp_def, reg: AllSparkRegistry, comp_name: str) -> dict:
    full_names = reg.guid_to_full_name.get(comp_name, {})
    return {
        "name": comp_def.name,
        "tooltip": comp_def.tooltip,
        "color": comp_def.color,
        "properties": [
            {
                "name": p.name,
                "guid": p.guid,
                "type": p.type_name,
                "full_name": full_names.get(p.guid, p.name),
                "tooltip": p.tooltip,
                "default": p.default_value,
            }
            for p in sorted(comp_def.properties.values(), key=lambda p: p.name)
        ],
    }


def _print_component_detail(comp_def, reg: AllSparkRegistry, comp_name: str) -> None:
    print(f"Component: {comp_def.name}")
    if comp_def.tooltip:
        print(f"  Tooltip: {comp_def.tooltip}")
    if comp_def.color:
        print(f"  Color:   {comp_def.color}")
    full_names = reg.guid_to_full_name.get(comp_name, {})
    print(f"\n  Properties ({len(comp_def.properties)}):")
    for p in sorted(comp_def.properties.values(), key=lambda p: p.name):
        fn = full_names.get(p.guid, p.name)
        type_str = f"  [{p.type_name}]" if p.type_name else ""
        print(f"    {fn}{type_str}  ({p.guid})")
        if p.tooltip:
            print(f"      {p.tooltip}")


def _print_node(node, indent: int, max_depth: int, depth: int = 0,
                *, full: bool = False) -> None:
    if depth >= max_depth:
        return
    prefix = "  " * indent
    key_info = f" [key={node.key_attribute}]" if node.key_attribute else ""
    print(f"{prefix}<{node.id}{key_info}>")
    for attr in node.attributes:
        val = attr.value
        if not full and len(val) > 80:
            val = val[:77] + "..."
        print(f"{prefix}  @{attr.id} ({attr.attr_type}) = {val}")
    for child in node.children:
        _print_node(child, indent + 1, max_depth, depth + 1, full=full)


def _print_resource_summary(r: LsxResource) -> None:
    total_nodes = sum(_count_nodes(n) for reg in r.regions for n in reg.nodes)
    _output.info(f"  {len(r.regions)} region(s), {total_nodes} total node(s)")


def _count_nodes(n) -> int:
    return 1 + sum(_count_nodes(c) for c in n.children)


def _print_effect_summary(e: EffectResource) -> None:
    total_components = sum(
        len(track.components)
        for tg in e.track_groups
        for track in tg.tracks
    )
    total_props = sum(
        len(comp.properties)
        for tg in e.track_groups
        for track in tg.tracks
        for comp in track.components
    )
    _output.info(f"  {len(e.track_groups)} track group(s), "
                 f"{total_components} component(s), {total_props} property(ies)")


def _compare_resources(a: LsxResource, b: LsxResource) -> None:
    a_nodes = sum(_count_nodes(n) for r in a.regions for n in r.nodes)
    b_nodes = sum(_count_nodes(n) for r in b.regions for n in r.nodes)
    _output.info(f"  Original: {len(a.regions)} regions, {a_nodes} nodes")
    _output.info(f"  Rebuilt:  {len(b.regions)} regions, {b_nodes} nodes")
    if a_nodes == b_nodes:
        _output.info(f"  {_green('Node counts match.')}")
    else:
        _output.error(f"  {_yellow('WARNING')}: Node count mismatch ({a_nodes} vs {b_nodes})")

    # Per-component attribute diff (for Effect region)
    a_comps = _get_effect_components(a)
    b_comps = _get_effect_components(b)
    if not a_comps and not b_comps:
        return
    a_by_id = {c.attr_value("ID", ""): c for c in a_comps}
    b_by_id = {c.attr_value("ID", ""): c for c in b_comps}
    matched = missing = extra = attr_diffs = 0
    for cid, ac in a_by_id.items():
        bc = b_by_id.get(cid)
        if not bc:
            missing += 1
            continue
        matched += 1
        a_props = {p.attr_value("FullName", ""): p.attr_value("Value", "")
                   for p in ac.children_with_id("Properties")
                   for p in p.children_with_id("Property")}
        b_props = {p.attr_value("FullName", ""): p.attr_value("Value", "")
                   for p in bc.children_with_id("Properties")
                   for p in p.children_with_id("Property")}
        for k in set(a_props) | set(b_props):
            if a_props.get(k) != b_props.get(k):
                attr_diffs += 1
    extra = len(set(b_by_id) - set(a_by_id))
    _output.info(f"  Components: {matched} matched, {missing} missing, {extra} extra, {attr_diffs} value diff(s)")


def _get_effect_components(resource: LsxResource) -> list:
    """Extract EffectComponent nodes from an LsxResource."""
    region = resource.region("Effect")
    if not region:
        return []
    for node in region.nodes:
        if node.id == "Effect":
            for child in node.children:
                if child.id == "EffectComponents":
                    return child.children
    return []


if __name__ == "__main__":
    main()
