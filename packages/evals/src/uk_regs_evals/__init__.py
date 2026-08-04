"""Golden-set + LLM-judge regression harness for the UK Regs pipeline."""

from uk_regs_evals.golden import (
    ExtractionCase,
    ExtractionExpected,
    LLMJudgeCase,
    ReconciliationCase,
    RetrievalCase,
    SuiteName,
    golden_dir,
    load_cases,
)
from uk_regs_evals.harnesses import (
    EvalReport,
    ExtractionHarness,
    LLMJudgeHarness,
    ReconciliationHarness,
    RetrievalHarness,
    run_all,
)
from uk_regs_evals.llm_judge import (
    Judge,
    JudgeCase,
    JudgeScore,
    LLMJudge,
    RubricJudge,
    judge_factory,
)
from uk_regs_evals.metrics import CaseResult, SuiteReport, summarize

__all__ = [
    # golden
    "ExtractionCase",
    "ExtractionExpected",
    "LLMJudgeCase",
    "ReconciliationCase",
    "RetrievalCase",
    "SuiteName",
    "golden_dir",
    "load_cases",
    # harnesses
    "EvalReport",
    "ExtractionHarness",
    "LLMJudgeHarness",
    "ReconciliationHarness",
    "RetrievalHarness",
    "run_all",
    # judge
    "Judge",
    "JudgeCase",
    "JudgeScore",
    "LLMJudge",
    "RubricJudge",
    "judge_factory",
    # metrics
    "CaseResult",
    "SuiteReport",
    "summarize",
]