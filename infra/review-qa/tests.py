"""Two fixed image-owned acceptance tests. Never load/import workload programs."""

import os
import sys
import unittest
from pathlib import Path


class PrototypeAcceptance(unittest.TestCase):
    def test_expected_fixture_output(self):
        self.assertEqual(
            (Path("/workspace") / sys.argv[1] / "prototype.txt").read_bytes(),
            (os.environ["REVIEW_EXPECTED"] + "\n").encode(),
        )

    def test_regular_bounded_utf8_output(self):
        file = Path("/workspace") / sys.argv[1] / "prototype.txt"
        self.assertTrue(file.is_file() and not file.is_symlink())
        data = file.read_bytes()
        self.assertTrue(0 < len(data) <= 4096)
        self.assertNotIn("\0", data.decode("utf-8"))


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PrototypeAcceptance)
    if os.environ.get("REVIEW_ZERO_TESTS") == "1":
        suite = unittest.TestSuite()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun != 2 or result.skipped:
        sys.exit(75)
    sys.exit(0 if result.wasSuccessful() else 1)
