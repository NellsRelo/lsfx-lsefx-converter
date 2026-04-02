# LSFX ↔ LSEFX Converter

Two-way converter between BG3 runtime `.lsfx` (binary LSF) and BG3 Toolkit `.lsefx` (XML) visual effect files.

## Overview

| Direction | Input | Output | Command |
|-----------|-------|--------|---------|
| Decompile | `.lsfx` (binary) | `.lsefx` (toolkit XML) | `decompile` |
| Compile   | `.lsefx` (toolkit XML) | `.lsfx` (binary) | `compile` |

The converter uses the BG3 Toolkit's **AllSpark definition files** to resolve the opaque property GUIDs in `.lsefx` files to human-readable property names used in the runtime LSF format, and vice versa.

## Prerequisites

1. **Python 3.13+**
2. **BG3 game installation** — the `--game` flag points to your BG3 install root and automatically resolves the AllSpark definition files (`ComponentDefinition.xcd` / `ModuleDefinition.xmd`) under `Data/Editor/Config/AllSpark/`

## Installation

```bash
cd lsfx-converter
pip install -e .
```

Or just install the dependency directly:

```bash
pip install lz4
```

## Usage

All commands that need AllSpark accept either `--game <BG3 root>` (recommended) or explicit `--xcd` / `--xmd` paths. If a **folder** is given instead of a file, all matching files are processed recursively.

### Compile: `.lsefx` → `.lsfx`

```bash
# Single file (Windows)
python -m converter compile effect.lsefx --game "H:\SteamLibrary\...\Baldurs Gate 3"

# Single file (Linux / macOS / Steam Deck)
python -m converter compile effect.lsefx --game "$HOME/.steam/steam/steamapps/common/Baldurs Gate 3"

# Bulk — converts every .lsefx in the folder (recursive), output mirrors directory structure
python -m converter compile path/to/effects_folder --game "...\Baldurs Gate 3" -o output_folder

# Dry run — list files that would be processed without converting
python -m converter compile path/to/effects_folder --game "...\Baldurs Gate 3" --dry-run
```

### Decompile: `.lsfx` → `.lsefx`

```bash
# Single file (Windows)
python -m converter decompile effect.lsfx --game "...\Baldurs Gate 3"

# Single file (Linux / macOS / Steam Deck)
python -m converter decompile effect.lsfx --game "$HOME/.steam/steam/steamapps/common/Baldurs Gate 3"

# Bulk
python -m converter decompile path/to/lsfx_folder --game "...\Baldurs Gate 3" -o output_folder
```

### Dump LSF Structure (no AllSpark needed)

```bash
python -m converter dump path/to/effect.lsfx

# Machine-readable JSON output
python -m converter dump path/to/effect.lsfx --json
```

### Inspect AllSpark Registry

```bash
# List all components and properties
python -m converter registry --game "...\Baldurs Gate 3"

# Search for a property or component by name
python -m converter registry --game "...\Baldurs Gate 3" --search Radius

# Show details for a specific component
python -m converter registry --game "...\Baldurs Gate 3" --component ParticleSystem

# JSON output
python -m converter registry --game "...\Baldurs Gate 3" --json
```

### Roundtrip Verification

```bash
python -m converter roundtrip path/to/effect.lsfx --game "...\Baldurs Gate 3" --keep
```

### Global Options

| Flag | Description |
|------|-------------|
| `-v` / `--verbose` | Show verbose diagnostics (AllSpark resolution details, etc.) |
| `-q` / `--quiet` | Suppress informational output; only show errors and results |
| `--no-color` | Disable colored output even when connected to a TTY |
| `--time` | Print elapsed time for the operation |
| `--json` | Emit machine-readable JSON output (for `dump` and `registry` commands) |

### Explicit AllSpark Paths (alternative to `--game`)

```bash
python -m converter compile effect.lsefx \
  --xcd path/to/ComponentDefinition.xcd \
  --xmd path/to/ModuleDefinition.xmd
```

## Architecture

```
converter/
├── __init__.py         Package marker
├── __main__.py         python -m converter entry point
├── cli.py              CLI argument parsing and dispatch
├── errors.py           Shared exception types (LsfParseError, TransformError)
├── lsx_model.py        LsxResource / LsxNode / LsxNodeAttribute data model
├── effect_model.py     EffectResource / Component / Property / RampChannelData model
├── lsf_reader.py       LSF binary format → LsxResource
├── lsf_writer.py       LsxResource → LSF binary format (v6, LZ4 compressed)
├── lsefx_io.py         .lsefx XML reader and writer
├── allspark.py         AllSpark .xcd / .xmd definition file parser
├── transform.py        Bidirectional LsxResource ↔ EffectResource conversion
└── _output.py          Verbosity control, colored output, and warning aggregation
```

### Data Flow

**Decompile (`.lsfx` → `.lsefx`):**
```
.lsfx binary
  → LSF parser (lsf_reader.py) → LsxResource
    → transform (transform.py) + AllSpark registry
      → EffectResource
        → XML writer (lsefx_io.py) → .lsefx file
```

**Compile (`.lsefx` → `.lsfx`):**
```
.lsefx XML
  → XML parser (lsefx_io.py) → EffectResource
    → transform (transform.py) + AllSpark registry
      → LsxResource
        → LSF writer (lsf_writer.py) → .lsfx binary
```

## Known Limitations

### Irrecoverable from binary (decompile only)

These are editor-only constructs that the BG3 runtime compiler strips when producing `.lsfx` files. They are preserved during `.lsefx` → `.lsfx` → `.lsefx` roundtrips if the source `.lsefx` contains them.

- **Muted tracks and components** — fully stripped during compilation; decompiled output only contains the unmuted components that survived into the binary.
- **Empty placeholder tracks** — the toolkit pads track groups with empty tracks for UI layout; no trace remains in the binary.
- **FreeTangentSpline vs Spline** — both compile to the same polynomial `FrameType=1`; the decompiler emits `Spline` for all spline channels.
- **`is_control_point` on keyframes** — an editor-only concept for Bézier handle editing; the binary stores evaluated polynomial segments, not control points.
- **Module-level muting** — individual modules within a component can be muted in the toolkit; this state is not preserved in the binary.
- **Original TrackGroup IDs** — the binary uses flat `Track` indices; the decompiler assigns sequential IDs starting from 1.
- **Original PropertyGroup GUIDs** — the toolkit assigns random instance GUIDs; the decompiler generates deterministic UUIDs from the component instance name.

### Best-effort reconstruction (decompile)

These elements are reconstructed from AllSpark definitions and binary metadata. The output is structurally correct but may differ cosmetically from the original toolkit file.

- **Track groups** — each unique `Track` index becomes its own track group with one track per component.
- **Phases** — extracted from the binary with correct duration/playcount; `definitionid` values are resolved from the XCD's `PhaseDefinition` entries (Lead In / Loop / Lead Out by position).
- **PropertyGroups** — always emitted as a single `"Property Group"` per component (matching the universal toolkit pattern).
- **Modules** — reconstructed by matching property GUIDs to module definitions in the XMD; the `Required` module is always index 0.
- **PlatformMetadata** — added to all ramp/keyframed properties with default expanded state (this is editor UI state that varies non-deterministically per instance).
- **`mutestateoverride`** — defaults to `"None"` for decompiled tracks (the binary doesn't preserve the original override state).

## Development

### Setup

```bash
cd lsfx-converter
python -m venv .venv313
# Windows: .\.venv313\Scripts\activate
# Linux/macOS: source .venv313/bin/activate
pip install -e ".[dev]"   # installs lz4 + pytest + pip-audit
```

### Running Tests

Unit tests (no game data required):

```bash
python -m pytest tests/ -v
```

### Integration / Compile-Parity Tests

Integration tests require local BG3 data. Copy `.env.example` to `.env` and fill in your paths:

```ini
# .env
BG3_LSFX_DIR=path/to/unpacked/Effects_Banks
BG3_LSEFX_DIR=path/to/Editor/Effects
BG3_GAME_DIR=path/to/Baldurs Gate 3
```

Then run the full suite — integration tests are skipped automatically when paths are missing:

```bash
python -m pytest tests/ -v
```

Or run only the parity tests:

```bash
python -m pytest tests/test_compile_parity.py tests/test_parity.py -v
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | All operations succeeded |
| 1 | Fatal error (bad arguments, missing files) |
| 2 | Partial failure in batch mode |
