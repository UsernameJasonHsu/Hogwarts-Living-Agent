import importlib.util
import json
from pathlib import Path

import pytest
from hogwarts.entrypoints.contracts import FormalDispatch
from pydantic import ValidationError


def gate_module():
    spec = importlib.util.spec_from_file_location("ci_gate", Path("scripts/ci_gate.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("missing", range(6))
def test_ci_requires_every_pinned_image(missing):
    m = gate_module()
    pins = {name: "sha256:" + "a" * 64 for name in m.IMAGE_VARIABLES}
    pins.pop(m.IMAGE_VARIABLES[missing])
    with pytest.raises(ValueError, match="ci_missing_or_unpinned_image"):
        m.image_pins(pins)


@pytest.mark.parametrize("state", ["skipped", "failure", "error", "duplicate", "zero"])
def test_ci_rejects_incomplete_or_nonpassing_junit(tmp_path, state):
    m = gate_module()

    def case(i):
        return f'<testcase classname="suite" name="case-{i}"></testcase>'

    cases = "".join(case(i) for i in range(1364))
    if state in {"skipped", "failure", "error"}:
        cases = cases.replace("</testcase>", f"<{state}/></testcase>", 1)
    elif state == "duplicate":
        cases = cases.replace('name="case-1"', 'name="case-0"')
    else:
        cases = ""
    path = tmp_path / "junit.xml"
    path.write_text(
        "<testsuites><testsuite>" + cases + "</testsuite></testsuites>", encoding="utf8"
    )
    with pytest.raises(ValueError):
        m.validate_case_set(
            m.parse_junit(path),
            {
                "schema_version": 1,
                "case_ids": [["suite", f"case-{i}"] for i in range(1364)],
                "required_suites": ["suite"],
                "approved_skips": {"linux": [], "windows": []},
            },
            "linux",
        )


def test_ci_configuration_covers_qa_and_actual_linux_images():
    m = gate_module()
    text = Path(".github/workflows/ci.yml").read_text(encoding="utf8")
    for name in m.IMAGE_VARIABLES:
        assert name in text
    assert "scripts/ci_gate.py --junit" in text and "tee test-results/pytest-raw.log" in text
    assert "REVIEW_ZERO_TESTS=1" in text and "REVIEW_EXPECTED=intentional failure" in text
    assert "contents: read" in text


def test_formal_schema_has_no_agent_commands_and_matches_json():
    assert (
        json.loads(Path("contracts/formal-dispatch.v1.schema.json").read_text(encoding="utf8"))
        == FormalDispatch.model_json_schema()
    )
    with pytest.raises(ValidationError):
        FormalDispatch(shell="true")
