"""New experiment, unchanged numerical gates; never reinterpret original cold.json."""

from copy import deepcopy

from paperforge_worker.pipelines.review_inputs import REVIEW_INPUT_VERSION

from evals.agent_writing.report import CONDITIONS as ORIGINAL_CONDITIONS
from evals.agent_writing.report import analyze as original_analyze

PROTOCOL = "frames-v2"
CONDITIONS = ("A_legacy", "B_dag_frames_serial", "C_dag_frames_parallel")
CONFIGURATIONS = {
    CONDITIONS[0]: ("legacy", 1),
    CONDITIONS[1]: ("dag_parallel", 1),
    CONDITIONS[2]: ("dag_parallel", 2),
}


def analyze(runs: list[dict]) -> dict:
    mapped = deepcopy(runs)
    for row in mapped:
        condition = row["condition"]
        if condition not in CONDITIONS or row.get("protocol") != PROTOCOL:
            raise ValueError("not a frames-v2 experiment")
        mode, width = CONFIGURATIONS[condition]
        if (row.get("writer_execution_mode"), row.get("frame_concurrency")) != (mode, width):
            raise ValueError("condition configuration mismatch")
        if row.get("body_concurrency") != 2 or row.get("evaluator_version") != REVIEW_INPUT_VERSION:
            raise ValueError("body width and evaluator version must be fixed")
        row["condition"] = ORIGINAL_CONDITIONS[CONDITIONS.index(condition)]
    report = original_analyze(mapped)
    report["conditions"] = {
        new: report["conditions"][old]
        for new, old in zip(CONDITIONS, ORIGINAL_CONDITIONS, strict=True)
    }
    a, b, c = [report["conditions"][k]["writing_seconds_median"] for k in CONDITIONS]
    report.update(
        {
            "protocol": PROTOCOL,
            "speed_gate_comparison": "B_to_C",
            "A_to_B_writing_time_reduction": 1 - b / max(a, 0.001),
            "A_to_C_writing_time_reduction_diagnostic_only": 1 - c / max(a, 0.001),
        }
    )
    return report
