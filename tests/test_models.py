from usd_pipeline.models import (
    PROCESS_FILE_KINDS,
    STEP_NAMES,
    StepOutcome,
    StepStatus,
)


def test_step_names_and_kinds():
    assert STEP_NAMES[0] == "detail"
    assert STEP_NAMES[-1] == "validation-report"
    assert "embedded-physics" in STEP_NAMES
    assert len(STEP_NAMES) == 8
    for k in PROCESS_FILE_KINDS:
        assert k in STEP_NAMES


def test_step_outcome_to_dict():
    out = StepOutcome(
        status=StepStatus.DONE,
        keys={"response": "r", "artifact": "a"},
        http_status=200,
        state="found",
        bytes=123,
    )
    d = out.to_dict()
    assert d["status"] == "done"
    assert d["keys"] == {"response": "r", "artifact": "a"}
    assert d["http_status"] == 200
    assert d["bytes"] == 123
    assert d["error"] is None


def test_step_outcome_defaults():
    out = StepOutcome(status=StepStatus.FAILED, error="boom")
    d = out.to_dict()
    assert d["status"] == "failed"
    assert d["keys"] == {}
    assert d["error"] == "boom"
