"""Static conflict detection tests with disposable fake mods."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / ".batman_modpack_deps"))
import batman_modpack_builder as app
import mod_conflicts


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class LuaScanTests(unittest.TestCase):
    def test_detects_globals_and_chaining_but_ignores_locals_and_comments(self) -> None:
        text = (
            "local original = ISToolTipInv.render\n"
            "function ISToolTipInv:render()\n  original(self)\nend\n"
            "ISInventoryPane.onMouseUp = function(self) end\n"
            "local Helper = {}\nfunction Helper.run() end\n"
            "function ISFoo:new(o)\n  o.render = function() end\nend\n"
            "-- function Commented.out() end\n"
            "--[[\nfunction Block.comment() end\n]]\n"
            "local function privateThing() end\n"
        )
        found = {item.target: item for item in mod_conflicts.scan_lua(text, "media/lua/client/a.lua")}
        self.assertEqual(set(found), {"ISToolTipInv.render", "ISInventoryPane.onMouseUp", "ISFoo.new"})
        self.assertTrue(found["ISToolTipInv.render"].chained)
        self.assertFalse(found["ISInventoryPane.onMouseUp"].chained)
        self.assertEqual(found["ISToolTipInv.render"].location, "media/lua/client/a.lua:2")

    def test_calling_the_function_is_not_chaining(self) -> None:
        found = mod_conflicts.scan_lua("function ISX.run()\n  local r = ISX.run()\nend\n", "x.lua")
        self.assertFalse(found[0].chained)

    def test_script_definitions_use_their_module(self) -> None:
        text = (
            "module Base {\n  item Axe\n  {\n  }\n"
            "  craftRecipe MakeThing\n  {\n    inputs { item 1 [Base.Nails], }\n  }\n}\n"
            "/* item Hidden */\nmodule Other { item Axe { } }\n"
        )
        names = [(kind, name) for kind, name, _ in mod_conflicts.scan_script(text, "s.txt")]
        self.assertEqual(names, [("Objet", "Base.Axe"), ("Recette", "Base.MakeThing"), ("Objet", "Other.Axe")])


class ConflictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mod-conflicts-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()

    def make_mod(self, workshop_id: str, mod_id: str, files: dict[str, str]) -> None:
        folder = self.root / workshop_id / "mods" / mod_id
        write(folder / "42" / "mod.info", f"id={mod_id}\nname={mod_id}\n")
        for relative, text in files.items():
            write(folder / relative, text)

    def test_reports_overrides_files_and_definitions_between_mods(self) -> None:
        self.make_mod("1", "Alpha", {
            "42/media/lua/client/Tooltip.lua": "function ISToolTipInv:render() end\n",
            "42/media/lua/shared/Shared.lua": "function SharedLib.go() end\n",
            "42/media/scripts/items.txt": "module Base { item Axe { } }\n",
            "42/media/lua/client/Chain.lua": "local old = ISPanel.render\nfunction ISPanel:render() old(self) end\n",
        })
        self.make_mod("2", "Beta", {
            "common/media/lua/client/Other.lua": "local o = ISToolTipInv.render\nISToolTipInv.render = function(s) o(s) end\n",
            "common/media/lua/shared/Shared.lua": "function SharedLib.go() end\n",
            "42/media/scripts/more.txt": "module Base {\n item Axe\n {\n }\n}\n",
            "42/media/lua/client/Chain2.lua": "local previous = ISPanel.render\nISPanel.render = function(s) previous(s) end\n",
        })
        catalog, errors = app.scan_catalog(self.root)
        self.assertFalse(errors)
        conflicts, scan_errors = app.analyse_conflicts(catalog, workers=1)
        self.assertFalse(scan_errors)
        by_target = {item.target: item for item in conflicts}
        self.assertEqual(
            set(by_target),
            {"ISToolTipInv.render", "ISPanel.render", "media/lua/shared/shared.lua", "Objet Base.Axe"},
        )
        self.assertEqual(by_target["ISToolTipInv.render"].severity, mod_conflicts.SEVERITY_HIGH)
        self.assertEqual(by_target["ISPanel.render"].severity, mod_conflicts.SEVERITY_LOW)
        self.assertEqual(by_target["media/lua/shared/shared.lua"].kind, mod_conflicts.KIND_LUA_FILE)
        self.assertEqual(by_target["Objet Base.Axe"].severity, mod_conflicts.SEVERITY_MEDIUM)
        self.assertEqual(conflicts[0].severity, mod_conflicts.SEVERITY_HIGH)
        self.assertEqual(len(by_target["ISToolTipInv.render"].mods), 2)

    def test_version_folder_replaces_common_file_within_one_mod(self) -> None:
        self.make_mod("1", "Solo", {
            "common/media/lua/client/A.lua": "function Solo.run() end\n",
            "42/media/lua/client/A.lua": "function Solo.run() end\n",
        })
        self.make_mod("2", "Other", {"42/media/lua/client/B.lua": "function Other.run() end\n"})
        catalog, _ = app.scan_catalog(self.root)
        self.assertEqual(app.analyse_conflicts(catalog, workers=1), ([], []))

    def test_conflicts_tab_analyses_manual_selection(self) -> None:
        from streamlit.testing.v1 import AppTest

        self.make_mod("1", "Alpha", {"42/media/lua/client/A.lua": "function ISFoo.bar() end\n"})
        self.make_mod("2", "Beta", {"42/media/lua/client/B.lua": "function ISFoo.bar() end\n"})
        with (
            patch.object(app, "WORKSHOP", self.root),
            patch.object(app, "SETTINGS_FILE", self.root / "settings.json"),
            patch.object(app, "DEFAULT_WORKSHOP_ROOT", self.root.parent / "output"),
            patch.object(app, "read_saved_lists", return_value={}),
        ):
            at = AppTest.from_string("import batman_modpack_builder as app\napp.main()").run()
            self.assertFalse(at.exception)
            at.radio(key="conflicts_source").set_value("Choix manuel").run()
            self.assertFalse(at.exception)

            def table() -> list[str]:
                editor = next(item for item in at.tabs[1].dataframe if item.key == "conflicts_editor")
                return editor.value["Nom"].tolist() if len(editor.value) else []

            self.assertEqual(table(), ["Alpha", "Beta"])
            at.text_input(key="conflicts_query").set_value("beta").run()
            self.assertEqual(table(), ["Beta"])
            at.text_input(key="conflicts_query").set_value("").run()
            # Data editor checkboxes cannot be clicked in AppTest; seed the state they write.
            at.session_state["conflicts_manual"] = ["1/Alpha/Alpha", "2/Beta/Beta"]
            at.checkbox(key="conflicts_only_checked").check().run()
            self.assertEqual(table(), ["Alpha", "Beta"])
            next(button for button in at.button if button.label == "Analyser les conflits").click().run()
            self.assertFalse(at.exception)
            results = next(item for item in at.tabs[1].dataframe if item.key != "conflicts_editor")
            self.assertEqual(results.value["Cible"].tolist(), ["ISFoo.bar"])
            next(button for button in at.button if button.label == "Tout décocher").click().run()
            self.assertEqual(at.session_state["conflicts_manual"], [])
            self.assertEqual(table(), [])

    def test_toggle_checked_applies_only_visible_rows(self) -> None:
        self.make_mod("1", "Alpha", {})
        self.make_mod("2", "Beta", {})
        catalog, _ = app.scan_catalog(self.root)
        edited = [{"Analyser": False}, {"Analyser": True}]
        self.assertEqual(
            app.toggle_checked(catalog, edited, {"1/Alpha/Alpha", "9/Hidden/Hidden"}, "Analyser"),
            {"2/Beta/Beta", "9/Hidden/Hidden"},
        )


if __name__ == "__main__":
    unittest.main()
