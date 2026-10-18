"""Pure deterministic manufacturing rules (no DB, no IO, no clock).

Typical service usage::

    from app.domain import FeatureSummary, PlanParams, QuoteParams, plan_process, compute_quote

    plan = plan_process(summary, PlanParams())            # plan.inputs_hash, plan.rules_version
    quote = compute_quote(plan, summary, QuoteParams(quantities=[1, 10, 100], quote_date=today))
    # store plan.model_dump(mode="json") / quote.model_dump(mode="json")

    status = evaluate(stored.inputs_hash, plan_inputs_hash(current_summary, stored_params),
                      stored_summary, current_summary)
"""

from .costing import (
    Alert,
    CostLine,
    OpCost,
    PlanMismatchError,
    QuoteParams,
    QuoteResult,
    TierResult,
    compute_quote,
)
from .hashing import (
    canonical_holes,
    canonical_json,
    canonical_summary,
    inputs_hash,
    plan_inputs_hash,
    quote_inputs_hash,
    sha256_hex,
)
from .materials import (
    MATERIAL_PRICE_VALID_UNTIL,
    MATERIAL_PRICE_VERSION,
    MATERIAL_TABLE_VERSION,
    MATERIALS,
    Material,
    UnknownMaterialError,
    find_material,
    get_material,
)
from .planning import (
    Assumption,
    Blank,
    Gap,
    Operation,
    PartMeasures,
    PlanParams,
    ProcessPlan,
    ToleranceSpec,
    parse_tolerance,
    plan_process,
)
from .rates import (
    CONSTANTS,
    MACHINE_TABLE_VERSION,
    MACHINES,
    PLAN_RULES_VERSION,
    QUOTE_RULES_VERSION,
    RATE_TABLE_VERSION,
    RULES_VERSION,
    SUBCONTRACTS,
    SourceRef,
)
from .schemas import FeatureSummary, HoleFeature, ToleranceNote
from .staleness import Change, Staleness, evaluate, summary_diff

__all__ = [
    "CONSTANTS",
    "MACHINES",
    "MACHINE_TABLE_VERSION",
    "MATERIALS",
    "MATERIAL_PRICE_VALID_UNTIL",
    "MATERIAL_PRICE_VERSION",
    "MATERIAL_TABLE_VERSION",
    "PLAN_RULES_VERSION",
    "QUOTE_RULES_VERSION",
    "RATE_TABLE_VERSION",
    "RULES_VERSION",
    "SUBCONTRACTS",
    "Alert",
    "Assumption",
    "Blank",
    "Change",
    "CostLine",
    "FeatureSummary",
    "Gap",
    "HoleFeature",
    "Material",
    "OpCost",
    "Operation",
    "PartMeasures",
    "PlanMismatchError",
    "PlanParams",
    "ProcessPlan",
    "QuoteParams",
    "QuoteResult",
    "SourceRef",
    "Staleness",
    "TierResult",
    "ToleranceNote",
    "ToleranceSpec",
    "UnknownMaterialError",
    "canonical_holes",
    "canonical_json",
    "canonical_summary",
    "compute_quote",
    "evaluate",
    "find_material",
    "get_material",
    "inputs_hash",
    "parse_tolerance",
    "plan_inputs_hash",
    "plan_process",
    "quote_inputs_hash",
    "sha256_hex",
    "summary_diff",
]
