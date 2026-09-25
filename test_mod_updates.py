"""Disposable Git repositories: python -m unittest test_mod_updates -v."""

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / ".batman_modpack_deps"))
import batman_modpack_builder as app


@unittest.skipUnless(shutil.which("git"), "Git is required")
class ModUpdatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mod-updates-test-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def write(self, name: str, content: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def baseline(self) -> str:
        app.initialize_mod_repository(self.root)
        return app.create_initial_mod_snapshot(self.root)

    def test_initialization_and_baseline_preserve_files(self) -> None:
        source = self.write("123/mods/Test/mod.info", "id=Test\n")
        self.assertFalse(app.mod_repository_root(self.root))
        revision = self.baseline()
        self.assertTrue(app.mod_repository_root(self.root))
        self.assertEqual(source.read_text(), "id=Test\n")
        self.assertEqual(app.collect_mod_changes(self.root, revision), [])
        app.initialize_mod_repository(self.root)
        with self.assertRaisesRegex(ValueError, "historique"):
            app.create_initial_mod_snapshot(self.root)
        self.assertEqual(app.run_mod_git(self.root, "rev-parse", "HEAD").strip(), revision)

    def test_changes_include_index_worktree_deletions_and_untracked(self) -> None:
        self.write("123/mods/Test/mod.info", "id=Test\n")
        deleted = self.write("123/mods/Test/media/deleted.lua", "old\n")
        self.write("123/mods/Test/media/edited.lua", "original\n")
        self.write(".gitignore", "*.ignored\n")
        revision = self.baseline()
        self.write("123/mods/Test/media/edited.lua", "staged\n")
        app.run_mod_git(self.root, "add", "--all")
        self.write("123/mods/Test/media/edited.lua", "current\n")
        deleted.unlink()
        self.write("456/mods/Nouveau/media/fichier été.txt", "nouveau\n")
        self.write("cache.ignored", "ignored\n")
        rows = app.collect_mod_changes(self.root, revision)
        by_path = {row["Fichier"]: row for row in rows}
        self.assertEqual(len(rows), 3)
        self.assertEqual(by_path["123/mods/Test/media/deleted.lua"]["État"], "Supprimé")
        self.assertEqual(by_path["123/mods/Test/media/edited.lua"]["Mod / dossier"], "123/mods/Test")
        self.assertIn("+current", app.mod_change_diff(self.root, revision, by_path["123/mods/Test/media/edited.lua"]))
        added = by_path["456/mods/Nouveau/media/fichier été.txt"]
        self.assertEqual(added["État"], "Non suivi")
        self.assertEqual(app.mod_change_diff(self.root, revision, added).splitlines(), ["nouveau"])

    def test_older_revision_detects_committed_updates(self) -> None:
        self.write("123/mods/Test/mod.info", "id=Test\n")
        revision = self.baseline()
        self.write("123/mods/Test/mod.info", "id=Test\nname=New\n")
        app.run_mod_git(self.root, "add", "--all")
        app.run_mod_git(self.root, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Update")
        self.assertEqual(len(app.mod_git_revisions(self.root)), 2)
        self.assertEqual(len(app.collect_mod_changes(self.root, revision)), 1)
        self.assertEqual(app.collect_mod_changes(self.root, "HEAD"), [])

    def test_nested_initialization_is_rejected(self) -> None:
        self.baseline()
        nested = self.root / "mods"
        nested.mkdir()
        with self.assertRaisesRegex(ValueError, "parent"):
            app.initialize_mod_repository(nested)
        self.assertFalse((nested / ".git").exists())

    def test_ui_initialization_scan_and_diff(self) -> None:
        from streamlit.testing.v1 import AppTest

        self.write("123/mods/Test/mod.info", "id=Test\n")
        with (
            patch.object(app, "WORKSHOP", self.root),
            patch.object(app, "DEFAULT_WORKSHOP_ROOT", self.root / "output"),
            patch.object(app, "scan_catalog", return_value=([], [])),
            patch.object(app, "read_saved_lists", return_value={}),
        ):
            at = AppTest.from_string("import batman_modpack_builder as app\napp.main()").run()
            self.assertEqual([tab.label for tab in at.tabs], ["Création du pack", "Mises à jour"])

            def click(label: str) -> None:
                next(button for button in at.button if button.label == label).click().run(timeout=30)
                self.assertFalse(at.exception)

            click("Initialiser Git dans ce dossier")
            click("Enregistrer le premier état de référence")
            self.write("123/mods/Test/mod.info", "id=Test\nname=Updated\n")
            click("Rechercher les changements")
            self.assertEqual(len(at.tabs[1].dataframe), 2)
            click("Afficher les différences")
            self.assertIn("+name=Updated", at.code[0].value)


if __name__ == "__main__":
    unittest.main()
