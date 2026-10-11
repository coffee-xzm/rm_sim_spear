"""Offline tests for ``rm_sim_spear.launch``."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rm_sim_spear.launch import resolve_game_executable


class ResolveGameExecutableTest(unittest.TestCase):
    def test_returns_absolute_path_of_an_existing_file(self) -> None:
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / "rm_sim_spear.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            self.assertEqual(resolve_game_executable(script), script.resolve())

    def test_expands_user_home(self) -> None:
        # The archived path is long; the user is expected to be able to write
        # `~/...` without the tool resolving it to a relative path first.
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / "pkg.sh"
            script.write_text("#!/bin/sh\n", encoding="utf-8")
            resolved = resolve_game_executable(Path("~") / str(script))
            self.assertTrue(resolved.is_absolute())
            self.assertEqual(resolved.name, "pkg.sh")

    def test_missing_file_fails_loudly(self) -> None:
        missing = Path("/tmp/rm_sim_spear_does_not_exist.sh")
        with self.assertRaises(SystemExit) as raised:
            resolve_game_executable(missing)
        self.assertIn("--game-executable", str(raised.exception))

    def test_directory_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit):
                resolve_game_executable(tmp)


if __name__ == "__main__":
    unittest.main()
