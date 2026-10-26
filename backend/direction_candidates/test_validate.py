import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from validate import BUNDLE, validate_bundle, validate_row


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.row = json.loads((BUNDLE / "teacher-directions.jsonl").read_text().splitlines()[0])

    def test_frozen_bundle(self):
        result = validate_bundle()
        self.assertEqual(sum(item["rows"] for item in result.values()), 2333)

    def test_invalid_row_values(self):
        changes = [
            {"confidence": -0.1}, {"confidence": 1.1}, {"confidence": float("nan")},
            {"confidence": True}, {"direction_deg": 360}, {"direction_deg": None},
            {"lat": float("inf")}, {"lon": 181}, {"status": "confirmed"},
            {"confidence_calibrated": True}, {"direction_convention": "sign_face"},
            {"status": "no_usable_road_candidate", "direction_deg": None, "confidence": 0.5},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_row(dict(self.row, **change))

    def test_unavailable_direction(self):
        row = dict(self.row, status="no_usable_road_candidate", direction_deg=None, confidence=0)
        validate_row(row)
        del row["direction_deg"]
        with self.assertRaises(ValueError):
            validate_row(row)

    def test_manifest_safety_and_byte_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / BUNDLE.name
            shutil.copytree(BUNDLE, bundle)
            path = bundle / "manifest.json"
            original = json.loads(path.read_text())
            for change in [{"production_approved": True}, {"purpose": "production"},
                           {"confidence_calibrated": True}, {"files": {}}]:
                with self.subTest(change=change):
                    manifest = copy.deepcopy(original)
                    manifest.update(change)
                    path.write_text(json.dumps(manifest))
                    with self.assertRaises(ValueError):
                        validate_bundle(bundle)
            path.write_text(json.dumps(original))
            with (bundle / "teacher-directions.jsonl").open("ab") as stream:
                stream.write(b"\n")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                validate_bundle(bundle)


if __name__ == "__main__":
    unittest.main()
