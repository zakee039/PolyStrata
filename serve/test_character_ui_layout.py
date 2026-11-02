"""Regression guards for Characters' three-panel navigation and existing field IDs."""
import unittest
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "web"


class IdCollector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []

    def handle_starttag(self, tag, attrs):
        ident = dict(attrs).get("id")
        if ident:
            self.ids.append(ident)


class CharacterLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "index.html").read_text(encoding="utf8")
        cls.js = (ROOT / "characters.js").read_text(encoding="utf8")
        cls.sprite = (ROOT / "sprite.svg").read_text(encoding="utf8")

    def test_three_tabs_and_unique_ids(self):
        collector = IdCollector()
        collector.feed(self.html)
        self.assertEqual(len(collector.ids), len(set(collector.ids)))
        for label in ("card", "world", "workflow"):
            self.assertIn(f'id="character-subtab-{label}"', self.html)
            self.assertIn(f'data-character-tab="{label}"', self.html)
            self.assertIn(f'id="character-panel-{label}"', self.html)
            self.assertIn(f'aria-controls="character-panel-{label}"', self.html)
        self.assertIn('function showCharacterSubTab(', self.js)
        self.assertIn('tab.onkeydown', self.js)

    def test_existing_fields_stay_in_correct_panel(self):
        start = self.html.index('id="character-panel-card"')
        world = self.html.index('id="character-panel-world"')
        flow = self.html.index('id="character-panel-workflow"')
        end = self.html.index('<!-- ============================ Images')
        card_panel = self.html[start:world]
        world_panel = self.html[world:flow]
        flow_panel = self.html[flow:end]
        for ident in ("card-name", "card-description", "card-personality",
                      "card-scenario", "card-greeting", "card-examples",
                      "user-persona", "persona-save", "card-import", "card-export"):
            self.assertIn(f'id="{ident}"', card_panel)
        for ident in ("lore-entries", "lore-add", "source-entries", "source-add-file",
                      "source-add-dir", "source-test-query", "source-test-btn"):
            self.assertIn(f'id="{ident}"', world_panel)
        for ident in ("card-workflow-path", "workflow-test-btn",
                      "workflow-test-results"):
            self.assertIn(f'id="{ident}"', flow_panel)
        self.assertIn('id="card-list"', self.html[:start])
        self.assertIn('id="card-save"', self.html[:start])

    def test_top_level_character_icon_exists(self):
        ET.parse(ROOT / "sprite.svg")
        self.assertIn('id="i-character"', self.sprite)
        self.assertIn('href="web/sprite.svg#i-character"', self.html)


if __name__ == "__main__":
    unittest.main()
