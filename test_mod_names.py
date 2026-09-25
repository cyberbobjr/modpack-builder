"""Display-name prefix tests with disposable source mods and packs."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).parent / ".batman_modpack_deps"))
import batman_modpack_builder as app


class ModNameTests(unittest.TestCase):
    def test_prefix_preserves_metadata_and_is_idempotent(self) -> None:
        original = b'\xef\xbb\xbfname=Example\r\nid=Example\r\nrequire=Other\r\n'
        updated = app.prefix_mod_name(original, 'MyPack', 'Fallback')
        self.assertEqual(updated, original.replace(b'name=Example', b'name=[MyPack] Example'))
        self.assertEqual(app.prefix_mod_name(updated, 'MyPack', 'Fallback'), updated)
        self.assertIn(b'name=[MyPack] Fallback', app.prefix_mod_name(b'id=Example', 'MyPack', 'Fallback'))

    def test_generation_migration_and_additions(self) -> None:
        with tempfile.TemporaryDirectory(prefix='mod-name-test-') as directory:
            root = Path(directory)
            source_root = root / 'sources'
            source = source_root / '123/mods/Example'
            source.mkdir(parents=True)
            source_info = source / 'mod.info'
            original = b'name=Example\nid=Example\n'
            source_info.write_bytes(original)
            catalog, errors = app.scan_catalog(source_root)
            self.assertFalse(errors)
            config = app.PackConfig(root / 'packs/MyPack', 'test_', 'test_pack', source_root)
            app.build(catalog, catalog, config, allow_internal_refs=True)
            infos = list(catalog[0].destination(config).rglob('mod.info'))
            self.assertEqual(len(infos), 2)
            for info in infos:
                self.assertEqual(app.read_info(info)['name'], '[MyPack] Example')
                self.assertEqual(app.read_info(info)['id'], catalog[0].new_id(config.prefix))
                info.write_bytes(info.read_bytes().replace(b'name=[MyPack] Example', b'name=Example'))
            manifest_path = config.variant / app.MANIFEST_NAME
            manifest_before = manifest_path.read_bytes()
            pack_before = (config.variant / 'mod.info').read_bytes()
            backup_before = (config.variant / 'mod.info.before-modpack-builder.bak').read_bytes()
            self.assertEqual(app.prefix_existing_mod_names(catalog, config), 2)
            self.assertEqual(app.prefix_existing_mod_names(catalog, config), 0)
            for info in infos:
                self.assertEqual(app.read_info(info)['name'], '[MyPack] Example')
                self.assertIn(b'name=Example', info.with_name('mod.info.before-name-prefix.bak').read_bytes())
            self.assertEqual(source_info.read_bytes(), original)
            self.assertEqual(manifest_path.read_bytes(), manifest_before)
            self.assertEqual((config.variant / 'mod.info').read_bytes(), pack_before)
            self.assertEqual((config.variant / 'mod.info.before-modpack-builder.bak').read_bytes(), backup_before)
            second = source_root / '456/mods/Second/42.20'
            second.mkdir(parents=True)
            (second / 'mod.info').write_text('name=Second\nid=Second\n', encoding='utf-8')
            catalog, _ = app.scan_catalog(source_root)
            selected = [mod for mod in catalog if mod.mod_id == 'Second']
            app.build(selected, catalog, config, allow_internal_refs=True)
            self.assertEqual(app.read_info(selected[0].destination(config) / '42.20/mod.info')['name'], '[MyPack] Second')
            self.assertFalse(app.load_pack_components(catalog, config)[1])


if __name__ == '__main__':
    unittest.main()
