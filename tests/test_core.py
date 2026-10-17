import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "run-pilot/scripts"))
from core import PilotError, Store, read_json, redact, validate_contract


def minimal():
    return {
        "schema_version": 1, "execution_id": "private-handle",
        "project": "/synthetic/project", "target": {"kind": "local"},
        "allowed_actions": ["observe"], "units": [{"id": "original", "argv": []}],
    }


class ContractTests(unittest.TestCase):
    def test_minimal_observe_contract_needs_no_project_framework(self):
        self.assertEqual(validate_contract(minimal())["units"][0]["id"], "original")

    def test_no_implicit_launch_authorization(self):
        doc = minimal()
        doc["allowed_actions"].append("launch")
        with self.assertRaises(PilotError):
            validate_contract(doc)

    def test_unsafe_identifiers_and_bad_types_are_rejected(self):
        for key, value in (("execution_id", "../escape"), ("max_concurrency", True),
                           ("target", "local"), ("allowed_actions", [{}]),
                           ("units", ["not-an-object"]), ("reserves", [])):
            doc = minimal()
            doc[key] = value
            with self.subTest(key=key), self.assertRaises(PilotError):
                validate_contract(doc)

    def test_scientific_config_not_rewritten(self):
        doc = minimal()
        doc["units"][0]["argv"] = [
            "python", "train.py", "--batch-size", "32", "--precision", "fp32",
            "--title", "空格 ; $(touch /not-executed)",
        ]
        before = copy.deepcopy(doc)
        validate_contract(doc)
        self.assertEqual(doc, before)

    def test_secrets_in_argv_refused(self):
        for flag in ("--token", "--api-key=hello", "--password", "--access_token=abc"):
            doc = minimal()
            doc["units"][0]["argv"] = ["python", "train.py", flag, "secret"]
            with self.subTest(flag=flag), self.assertRaises(PilotError):
                validate_contract(doc)

    def test_cycles_and_unknown_dependencies_rejected(self):
        doc = minimal()
        doc["units"] = [
            {"id": "a", "depends_on": ["b"]}, {"id": "b", "depends_on": ["a"]},
        ]
        with self.assertRaises(PilotError):
            validate_contract(doc)
        doc["units"][1]["depends_on"] = ["not-approved"]
        with self.assertRaises(PilotError):
            validate_contract(doc)

    def test_nonfinite_resource_limits_refused(self):
        doc = minimal()
        doc["reserves"] = {"gpu_mb": float("nan")}
        with self.assertRaises(PilotError):
            validate_contract(doc)

    def test_redaction_nested_credentials_and_url_passwords(self):
        result = redact({"api_key": "sensitive", "message": "token=abc https://a:pw@example.invalid/path"})
        self.assertNotIn("sensitive", str(result))
        self.assertNotIn("token=abc", str(result))
        self.assertNotIn("a:pw@", str(result))

    def test_bounded_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "big.json"
            path.write_text(json.dumps("x" * 2000))
            with self.assertRaises(PilotError):
                read_json(path, limit=100)

    def test_contract_immutable_and_state_version_retained(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "private"
            store = Store(root)
            doc = minimal()
            store.save_contract(doc)
            store.save_contract(doc)
            doc["allowed_actions"].append("launch")
            doc["scope_approved"] = True
            with self.assertRaises(PilotError):
                store.save_contract(doc)
            store.db.execute("PRAGMA user_version=99")
            store.close()
            with self.assertRaises(PilotError):
                Store(root)
            database = sqlite3.connect(str(root / "state.sqlite3"))
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 99)
            self.assertEqual(database.execute("SELECT COUNT(*) FROM contracts").fetchone()[0], 1)
            database.close()


if __name__ == "__main__":
    unittest.main()
