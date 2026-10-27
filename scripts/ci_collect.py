"""Collect exact JUnit-compatible IDs without executing tests or approving skips."""

import argparse
import json
from pathlib import Path

import pytest
from _pytest.junitxml import bin_xml_escape, mangle_test_address


class Collection:
    def __init__(self):
        self.ids = []

    def pytest_collection_finish(self, session):
        for item in session.items:
            names = mangle_test_address(item.nodeid)
            self.ids.append([bin_xml_escape(".".join(names[:-1])), bin_xml_escape(names[-1])])


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    plugin = Collection()
    code = pytest.main(["--collect-only", "-q"], plugins=[plugin])
    if code:
        raise SystemExit(code)
    if not plugin.ids or len({tuple(x) for x in plugin.ids}) != len(plugin.ids):
        raise SystemExit("invalid collection")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(sorted(plugin.ids), indent=2), encoding="utf8")


if __name__ == "__main__":
    main()
