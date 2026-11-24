"""Infrastructure & CI/CD: golden-dataset regression suite."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from google.adk.evaluation.eval_set import EvalSet

from evals import offline_eval
from evals.build_golden_evalset import build

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "evals" / "golden_evalset.json"


def test_golden_evalset_is_valid_adk_evalset():
  data = json.loads(GOLDEN.read_text())
  eval_set = EvalSet.model_validate(data)
  assert len(eval_set.eval_cases) >= 10
  assert {c.eval_id for c in eval_set.eval_cases} >= {"prompt_injection", "restart_crash_loop"}


def test_golden_evalset_is_up_to_date_with_builder():
  assert json.loads(GOLDEN.read_text()) == build()


def test_eval_config_has_thresholds():
  cfg = json.loads((ROOT / "evals" / "test_config.json").read_text())
  assert cfg["criteria"]["tool_trajectory_avg_score"]["threshold"] >= 0.8


@pytest.mark.parametrize("case", offline_eval.load_cases(), ids=lambda c: c["eval_id"])
def test_golden_case_passes_offline(case):
  result = offline_eval.evaluate_case(case)
  assert result["passed"], result["failures"]


def test_overall_offline_score_is_perfect():
  report = offline_eval.run()
  assert report["score"] == 1.0


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("RUN_LIVE_EVALS"), reason="set RUN_LIVE_EVALS=1 with GCP creds")
@pytest.mark.asyncio
async def test_live_agent_evaluation_against_golden_set():
  from google.adk.evaluation.agent_evaluator import AgentEvaluator  # pylint: disable=import-outside-toplevel

  await AgentEvaluator.evaluate(
      agent_module="agent_app",
      eval_dataset_file_path_or_dir=str(GOLDEN),
      num_runs=1,
  )
