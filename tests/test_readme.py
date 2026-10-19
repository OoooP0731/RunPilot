"""Keep the README's project-facing JSON examples aligned with schema 1."""

import json
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "run-pilot/scripts"))
from core import validate_contract


class ReadmeInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        cls.examples = [
            json.loads(raw)
            for raw in re.findall(r"```json\n(.*?)\n```", cls.readme, re.S)
        ]
        cls.progress = next(item for item in cls.examples if "step" in item)
        cls.report = next(item for item in cls.examples if "accuracy" in item)
        cls.mapping = next(item for item in cls.examples if "progress" in item)

    def test_documented_mapping_is_a_valid_unit_contract(self):
        unit = dict(self.mapping, id="worker", argv=[])
        doc = {
            "schema_version": 1,
            "execution_id": "readme-example",
            "project": "/absolute/project",
            "target": {"kind": "local"},
            "allowed_actions": ["observe"],
            "units": [unit],
        }
        self.assertEqual(validate_contract(doc)["units"][0], unit)

    def test_progress_fields_match_the_example(self):
        mapping = self.mapping["progress"]
        for key in ("step_field", "total_field", "phase_field", "label_field"):
            self.assertIn(mapping[key], self.progress)
        self.assertLessEqual(self.progress[mapping["step_field"]],
                             self.progress[mapping["total_field"]])

    def test_completion_fields_match_the_example(self):
        mapping = self.mapping["completion"]
        for key in mapping["required_fields"]:
            self.assertIn(key, self.report)
        self.assertEqual(self.report[mapping["success_field"]],
                         mapping["success_value"])

    def test_local_document_links_exist(self):
        for link in re.findall(r"\[[^\]]*\]\(([^)]+)\)", self.readme):
            if "://" in link or link.startswith("#"):
                continue
            with self.subTest(link=link):
                self.assertTrue((ROOT / link.split("#")[0]).is_file())


if __name__ == "__main__":
    unittest.main()
