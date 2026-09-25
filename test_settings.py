"""Settings and discovery tests using disposable folders only."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / ".batman_modpack_deps"))
import batman_modpack_builder as app


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mod-settings-test-")
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)
        self.settings = self.root / "settings.json"
        self.settings_patch = patch.object(app, "SETTINGS_FILE", self.settings)
        self.settings_patch.start()
        self.addCleanup(self.settings_patch.stop)

    def test_settings_roundtrip_and_invalid_path(self) -> None:
        app.save_source_settings(self.root)
        self.assertEqual(app.load_source_settings(), self.root)
        with self.assertRaises(ValueError):
            app.save_source_settings(self.root / "missing")
        self.assertEqual(app.load_source_settings(), self.root)
        self.settings.write_text("[]", encoding="utf-8")
        with self.assertRaises(ValueError):
            app.load_source_settings()

    def test_scoped_search_and_limit(self) -> None:
        workshop = self.root / "SteamLibrary/steamapps/workshop/content/108600"
        workshop.mkdir(parents=True)
        ignored = self.root / ".git/steamapps/workshop/content/108600"
        ignored.mkdir(parents=True)
        found, truncated, inaccessible = app.search_workshop_paths(self.root)
        self.assertEqual(found, [workshop])
        self.assertFalse(truncated)
        self.assertEqual(inaccessible, 0)
        self.assertTrue(app.search_workshop_paths(self.root, limit=1)[1])

    def test_discovery_reads_library_manifest(self) -> None:
        steam = self.root / "Programs" / "Steam"
        (steam / "steamapps").mkdir(parents=True)
        library = self.root / "Custom library"
        workshop = library / "steamapps/workshop/content/108600"
        workshop.mkdir(parents=True)
        path_value = str(library).replace("\\", "\\\\")
        (steam / "steamapps/libraryfolders.vdf").write_text(
            '"libraryfolders" { "1" { "path" "' + path_value + '" } }', encoding="utf-8"
        )
        self.assertIn(workshop, app.discover_workshop_paths({steam}))

    def test_output_inside_selected_source_is_rejected(self) -> None:
        config = app.PackConfig(self.root / "output", "test_", "test_pack", self.root)
        self.assertTrue(any("sources Workshop" in error for error in app.validate_config(config)))

    def test_ui_path_change_updates_catalog_and_git_and_survives_restart(self) -> None:
        from streamlit.testing.v1 import AppTest

        first = self.root / "first"
        second = self.root / "second"
        for directory, mod_id in ((first, "First"), (second, "Second")):
            folder = directory / "123/mods" / mod_id
            folder.mkdir(parents=True)
            (folder / "mod.info").write_text(f"id={mod_id}\nname={mod_id}\n", encoding="utf-8")
        with (
            patch.object(app, "WORKSHOP", first),
            patch.object(app, "DEFAULT_WORKSHOP_ROOT", self.root / "output"),
            patch.object(app, "read_saved_lists", return_value={}),
        ):
            at = AppTest.from_string("import batman_modpack_builder as app\napp.main()").run()
            self.assertFalse(at.exception)
            at.session_state["selected_roots"] = ["old-selection"]
            next(widget for widget in at.text_input if widget.key == "settings_source_input").set_value(str(second)).run()
            next(button for button in at.button if button.label == "Enregistrer le dossier").click().run()
            self.assertFalse(at.exception)
            self.assertEqual(app.load_source_settings(), second)
            self.assertEqual(at.session_state["selected_roots"], [])
            self.assertEqual(next(widget for widget in at.text_input if widget.key == "updates_root").value, str(second))
            self.assertIn("Second", str(at.tabs[0].dataframe[0].value))
            restarted = AppTest.from_string("import batman_modpack_builder as app\napp.main()").run()
            self.assertFalse(restarted.exception)
            self.assertEqual(restarted.session_state["source_root"], str(second))


if __name__ == "__main__":
    unittest.main()
