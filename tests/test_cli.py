"""Unit tests for CLI error handling — no game data required."""

import tempfile
from pathlib import Path

import pytest


class TestCliErrors:
    def test_decompile_nonexistent_file(self):
        """CLI should exit(1) for a missing input file."""
        from converter.cli import main
        with pytest.raises(SystemExit) as exc_info:
            main(["decompile", "--game", ".", "/nonexistent/path/fake.lsfx"])
        assert exc_info.value.code != 0

    def test_compile_wrong_extension(self):
        """CLI should error when compiling a .lsfx file (should be .lsefx)."""
        from converter.cli import main
        with tempfile.NamedTemporaryFile(suffix=".lsfx", delete=False) as f:
            f.write(b"dummy")
            f.flush()
            with pytest.raises(SystemExit) as exc_info:
                main(["compile", "--game", ".", f.name])
            assert exc_info.value.code != 0

    def test_decompile_wrong_extension(self):
        """CLI should error when decompiling a .lsefx file (should be .lsfx)."""
        from converter.cli import main
        with tempfile.NamedTemporaryFile(suffix=".lsefx", delete=False) as f:
            f.write(b"dummy")
            f.flush()
            with pytest.raises(SystemExit) as exc_info:
                main(["decompile", "--game", ".", f.name])
            assert exc_info.value.code != 0

    def test_dry_run_still_requires_registry(self):
        """--dry-run still requires registry args (validation happens first)."""
        from converter.cli import main
        with tempfile.TemporaryDirectory() as tmpdir:
            fake = Path(tmpdir) / "test.lsfx"
            fake.write_bytes(b"dummy")
            with pytest.raises(SystemExit) as exc_info:
                main(["decompile", "--dry-run", str(fake)])
            assert exc_info.value.code != 0

    def test_missing_registry_args(self):
        """CLI should error if neither --game nor --xcd+--xmd provided for non-dry-run."""
        from converter.cli import main
        with tempfile.NamedTemporaryFile(suffix=".lsfx", delete=False) as f:
            f.write(b"dummy")
            f.flush()
            with pytest.raises(SystemExit) as exc_info:
                main(["decompile", f.name])
            assert exc_info.value.code != 0
