"""Concept planning authorized by an exact workflow input, never an invented assistant root."""
from __future__ import annotations

import copy
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..ai import design_agent, retry as call_retry
from ..ai.design_contracts import DesignDecision, DesignPlanContent, DesignRef
from ..ai.providers.base import ProviderError
from ..errors import AppError, conflict
from ..models import NodeRun, Project, ProjectMember, User, WorkflowDesignPlanVersion, WorkflowRun
from ..security import project_access
from ..workflow.engine import NodeContext, NodeFailure, NodeOutcome
from . import ai as ai_svc, design_plans, design_spec
from .assistant_tool_inputs import ResolvedDesignPlan

TOOLS = ('spec_extract', 'modeling', 'assembly_from_spec')


def reference(row: WorkflowDesignPlanVersion) -> DesignRef:
    return DesignRef(plan_id=row.plan_id, version_id=row.id, version=row.version, sha256=row.sha256)


def _scope(db: Session, user: User, run_id: uuid.UUID, node_run_id: uuid.UUID) -> tuple[WorkflowRun, NodeRun]:
    run = db.get(WorkflowRun, run_id, populate_existing=True)
    node = db.get(NodeRun, node_run_id, populate_existing=True)
    if run is None or node is None or node.run_id != run.id or run.created_by != user.id:
        raise conflict('design_scope_mismatch', 'the design belongs to another workflow or owner')
    db.get(Project, run.project_id, populate_existing=True)
    db.get(ProjectMember, {'project_id': run.project_id, 'user_id': user.id}, populate_existing=True)
    access = project_access(db, user, run.project_id, 'design')
    if access.project.archived_at is not None or access.project.lifecycle != 'active':
        raise conflict('project_not_active', 'workflow design requires an active project')
    if run.cancel_requested or run.status == 'cancelled' or run.paused:
        raise conflict('design_plan_cancelled', 'this design workflow was cancelled or paused')
    return run, node


def _graph_node(run: WorkflowRun, node: NodeRun) -> dict[str, Any]:
    found = [n for n in (run.graph_snapshot or {}).get('nodes', []) if n.get('id') == node.node_id]
    if len(found) != 1 or found[0].get('type') != node.node_type:
        raise conflict('design_scope_mismatch', 'the design node is absent from its frozen workflow graph')
    return found[0]


def _source(db: Session, run: WorkflowRun, node: NodeRun) -> dict[str, Any]:
    """Only the exact frozen text-input binding can authorize a workflow concept."""
    graph_node = _graph_node(run, node)
    if node.node_type != 'concept_design':
        raise conflict('design_scope_mismatch', 'this node cannot authorize concept design')
    incoming = [e for e in run.graph_snapshot.get('edges', [])
                if e.get('target') == node.node_id and e.get('target_port') == 'text']
    if len(incoming) != 1 or incoming[0].get('source_port') != 'text':
        raise conflict('design_source_invalid', 'connect one original text input to concept design')
    source_node = db.scalar(select(NodeRun).where(NodeRun.run_id == run.id,
        NodeRun.node_id == incoming[0].get('source')).execution_options(populate_existing=True))
    if source_node is None or source_node.node_type != 'text_input' or source_node.status != 'succeeded':
        raise conflict('design_source_invalid', 'concept design requires a completed original text input')
    source_graph = _graph_node(run, source_node)
    value = (source_graph.get('params') or {}).get('text')
    if not isinstance(value, str) or not value.strip():
        raise AppError(422, 'missing_input', 'enter a fresh design request in the text input')
    if (source_node.outputs or {}).get('text') != {'type': 'text', 'text': value}:
        raise conflict('design_hash_mismatch', 'the text output differs from its original frozen input')
    try:
        model = ai_svc.run_model_ref(run, 'planning', (graph_node.get('params') or {}).get('model_config_id'))
    except ProviderError as exc:
        raise AppError(409, exc.code, exc.message) from exc
    return {'kind': 'workflow_text_input', 'run_id': str(run.id), 'node_run_id': str(node.id), 'node_id': node.node_id,
        'input_node_run_id': str(source_node.id), 'input_node_id': source_node.node_id, 'text': value,
        'graph_sha256': design_plans.digest(run.graph_snapshot), 'inputs_sha256': design_plans.digest(run.inputs),
        'execution_policy': run.execution_policy, 'planning_model_sha256': design_plans.digest(model),
        'materials': [], 'constraints': [], 'parameter_overrides': [], 'user_responses': []}


def _authorization(row: WorkflowDesignPlanVersion, *, delegated: bool) -> dict[str, Any]:
    return {'kind': 'workflow_design_request', 'project_id': str(row.project_id), 'user_id': str(row.user_id),
        'run_id': str(row.run_id), 'node_run_id': str(row.node_run_id), 'node_id': row.node_id,
        'execution_policy': row.source_snapshot['execution_policy'], 'tools': list(TOOLS), 'delegated': delegated}


def _verify(db: Session, user: User, row: WorkflowDesignPlanVersion, ref: DesignRef) -> tuple[WorkflowRun, NodeRun]:
    if row.plan_id != ref.plan_id or row.version != ref.version or row.plan_id != uuid.uuid5(row.node_run_id, 'workflow-design-plan'):
        raise conflict('design_scope_mismatch', 'the exact workflow design version does not match its node')
    if (row.sha256 != ref.sha256 or design_plans.digest(row.content) != ref.sha256
            or design_plans.digest(row.source_snapshot) != row.source_sha256):
        raise conflict('design_hash_mismatch', 'the design or original source digest no longer matches')
    run, node = _scope(db, user, row.run_id, row.node_run_id)
    if row.user_id != user.id or row.project_id != run.project_id or row.node_id != node.node_id:
        raise conflict('design_scope_mismatch', 'the design owner, project or originating node does not match')
    if node.status not in ('running', 'succeeded'):
        raise conflict('design_not_authorized', 'the originating design node has no adopted execution result')
    if _source(db, run, node) != row.source_snapshot:
        raise conflict('design_hash_mismatch', 'the workflow input, graph, policy or frozen model changed')
    delegated = row.authorization.get('delegated')
    if type(delegated) is not bool or row.authorization != _authorization(row, delegated=delegated):
        raise conflict('design_not_authorized', 'the stored workflow authorization is not valid')
    return run, node


def _running(node: NodeRun, *, attempt: int | None = None) -> None:
    if (node.status != 'running' or attempt is not None and node.attempt != attempt
            or node.lease_until is None or node.lease_until <= datetime.now(UTC)):
        raise conflict('lease_lost', 'this attempt no longer holds the workflow node')


def resolve_authorized_design(db: Session, user: User, design_ref: DesignRef | dict, *, project_id: uuid.UUID,
                              node_run_id: uuid.UUID | None, tool_kind: str) -> ResolvedDesignPlan:
    """A persisted row is necessary but never sufficient: bind each consumer to this run's exact edge."""
    try:
        ref = design_ref if isinstance(design_ref, DesignRef) else DesignRef.model_validate(design_ref)
    except (ValidationError, TypeError, ValueError) as exc:
        raise conflict('design_scope_mismatch', 'the workflow design reference is invalid') from exc
    row = db.get(WorkflowDesignPlanVersion, ref.version_id)
    if row is None or row.user_id != user.id or row.project_id != project_id:
        raise AppError(404, 'design_plan_not_found', 'design plan not found')
    run, source_node = _verify(db, user, row, ref)
    if not row.authorization['delegated'] or tool_kind not in TOOLS or any(q.get('required') for q in row.content.get('questions', [])):
        raise conflict('design_not_authorized', 'this exact design needs a user choice before building')
    consumer = db.get(NodeRun, node_run_id, populate_existing=True) if node_run_id else None
    if consumer is None or consumer.run_id != run.id:
        raise conflict('design_scope_mismatch', 'workflow designs can only execute inside their originating run')
    _running(consumer)
    _graph_node(run, consumer)
    if tool_kind == 'spec_extract':
        valid = consumer.id == source_node.id
    else:
        valid = consumer.node_type == 'assembly_from_spec' and source_node.status == 'succeeded' and any(
            e.get('source') == source_node.node_id and e.get('source_port') == 'spec'
            and e.get('target') == consumer.node_id and e.get('target_port') == 'spec'
            for e in run.graph_snapshot.get('edges', []))
    if not valid:
        raise conflict('design_scope_mismatch', 'the consuming node is not bound to this design output')
    return ResolvedDesignPlan(ref=ref, content=DesignPlanContent.model_validate(row.content),
        source_snapshot=copy.deepcopy(row.source_snapshot), source_sha256=row.source_sha256,
        authorization=copy.deepcopy(row.authorization))


def _capabilities() -> dict[str, Any]:
    from ..domain.materials import MATERIALS, MATERIAL_TABLE_VERSION

    return {'kind': 'system_manufacturing_capabilities', 'material_table_version': MATERIAL_TABLE_VERSION,
        'machining_estimate_materials': [{'id': m.id, 'family': m.family, 'name': m.name_en} for m in MATERIALS.values()],
        'processes': ['saw_blank', 'vmc_rough_finish', 'drilling', 'edge_finish', 'deburr', 'inspection'],
        'limits': 'Machining estimates only; no certified strength, motion, welding, moulding or supplier price inference.',
        'material_selection': 'If the user delegates or names only a material family, choose a suitable explicit grade '
            'from these capabilities and disclose the grade as an AI decision. Preserve all explicit human material '
            'grades and prohibitions, even when unsupported for pricing. This context is not human source evidence.'}


def _projection(row: WorkflowDesignPlanVersion) -> dict[str, Any]:
    return {'ref': reference(row).model_dump(mode='json'), 'title': row.content['title'],
        'summary': row.content.get('summary') or '', 'status': 'adopted' if row.authorization['delegated'] else 'needs_input',
        'decisions': row.content.get('decisions') or [],
        'questions': [{k: v for k, v in q.items() if k != 'field_path'} for q in row.content.get('questions') or []],
        'can_answer': False, 'simulated': bool(row.provenance.get('simulated'))}


def _outcome(ctx: NodeContext, row: WorkflowDesignPlanVersion) -> NodeOutcome:
    plan = _projection(row)
    outputs: dict[str, Any] = {'design_plan': plan, 'simulated': plan['simulated']}
    if not row.authorization['delegated']:
        ctx.step('concept_design', ok=False, status='needs_input', questions=len(plan['questions']))
        return NodeOutcome(outputs=outputs, steps=ctx.steps, failure={
            'code': 'design_choice_required',
            'message': 'This design needs your choice. Add the requested choices to the text input and start a new run.',
            'details': {'design_ref': plan['ref'], 'questions': plan['questions'], 'recovery': 'start_new_run'},
            'recovery': 'start_new_run'})
    access = project_access(ctx.db, ctx.user, ctx.project.id, 'design')
    resolved = resolve_authorized_design(ctx.db, ctx.user, reference(row), project_id=ctx.project.id,
        node_run_id=ctx.node_run.id, tool_kind='spec_extract')
    spec, version = design_spec.compile_design_spec(ctx.db, access, resolved, policy=ctx.policy,
        node_run_id=ctx.node_run.id)
    from .orchestration import spec_brief

    outputs['spec'] = spec_brief(ctx.db, version, spec)
    ctx.step('concept_design', status='adopted', spec=outputs['spec']['code'], version=version.version_no,
             definitions=len(row.content['definitions']), decisions=len(plan['decisions']), simulated=plan['simulated'])
    return NodeOutcome(outputs=outputs, steps=ctx.steps)


def _validate_content(content: DesignPlanContent, source: dict[str, Any]) -> DesignPlanContent:
    """The fixed manufacturing graph consumes an assembly even when assistant planning permits parts."""
    plan = design_plans.validate_planner_content(content, source)
    root = next(definition for definition in plan.definitions if definition.key == plan.root)
    if root.kind != 'assembly':
        raise AppError(422, 'design_root_kind_mismatch', 'this manufacturing workflow requires an assembly root')
    return plan


def concept_design(ctx: NodeContext) -> NodeOutcome:
    """Plan once for the original input; compile without loosening any downstream geometry gate."""
    calls: list[Any] = []
    try:
        run, node = _scope(ctx.db, ctx.user, ctx.run.id, ctx.node_run.id)
        if run.project_id != ctx.project.id or ctx.policy != run.execution_policy:
            raise conflict('design_scope_mismatch', 'the design context belongs to another project or policy')
        _running(node, attempt=ctx.attempt)
        source = _source(ctx.db, run, node)
        if ctx.inputs != {'text': {'type': 'text', 'text': source['text']}}:
            raise conflict('design_hash_mismatch', 'the supplied design text differs from its original workflow input')
        prior = ctx.db.scalar(select(WorkflowDesignPlanVersion).where(WorkflowDesignPlanVersion.node_run_id == node.id))
        if prior is not None:
            _verify(ctx.db, ctx.user, prior, reference(prior))
            return _outcome(ctx, prior)
        graph_node = _graph_node(run, node)
        model_ref = ai_svc.run_model_ref(run, 'planning', (graph_node.get('params') or {}).get('model_config_id'))
        provider, _ = ai_svc.provider_for(ctx.db, 'planning', model_ref)

        def live_guard() -> None:
            try:
                current_run, current_node = _scope(ctx.db, ctx.user, run.id, node.id)
                _running(current_node, attempt=ctx.attempt)
                if _source(ctx.db, current_run, current_node) != source:
                    raise conflict('design_hash_mismatch', 'the original design input changed during planning')
            except AppError as exc:
                raise ProviderError(exc.code, exc.message, retryable=False) from exc

        with call_retry.deadline_scope(time.monotonic() + 300, check=live_guard) as limits:
            def stop() -> None:
                reason = limits.stop_reason(calls=False)
                remaining = limits.remaining_s()
                # Reserve applies to a NEW request, not adoption of a timely final reply or the last allowed call.
                if reason is not None and (reason.kind in ('cancelled', 'lease_lost') or remaining is not None and remaining <= 0):
                    raise reason.error()

            capabilities = _capabilities()
            outcome = design_agent.run_design(provider, {'source': source, 'previous_plan': None, 'response': None,
                'execution_policy': ctx.policy, 'manufacturing_capabilities': capabilities}, stop=stop,
                root_mode='assembly', validate=lambda content: _validate_content(content, source))
            calls = outcome.calls
            stop()
            if outcome.error or outcome.reply is None:
                error = outcome.error or {'code': 'invalid_design_plan', 'message': 'no valid design was returned'}
                raise NodeFailure(error['code'], error['message'], retryable=bool(error.get('retryable')),
                                  details=error.get('details'), calls=calls)
            content = outcome.reply.content
            for decision in content.decisions:
                decision.origin = 'ai_design'
            # Preserve the literal human constraint; a broader material family never confirms an AI-selected grade.
            for constraint in content.hard_constraints:
                content.decisions.append(DesignDecision(id=str(uuid.uuid5(node.id, 'user:' + constraint.id)),
                    key='user:' + constraint.id, label=constraint.label, value=constraint.source_quote,
                    reason=constraint.source_quote, origin='user_input'))
            plan_id = uuid.uuid5(node.id, 'workflow-design-plan')
            for question in content.questions:
                question.id = str(uuid.uuid5(plan_id, '1:' + question.key))
            design_plans._questions(content)
            waiting = outcome.reply.wait_for_user or any(q.required for q in content.questions)
            if waiting and not content.questions:
                raise AppError(422, 'invalid_design_plan', 'a customization wait requires actual design questions')
            content = _validate_content(content, source)
            data = content.model_dump(mode='json')
            if len(json.dumps(data, ensure_ascii=False).encode()) > design_plans.MAX_PLAN_BYTES:
                raise AppError(422, 'context_over_budget', 'the design is too large to persist or execute')
            stop()
            # Serialize only persistence. Provider calls never hold a run/node lock or prevent cancellation/heartbeats.
            from .lifecycle import require_active

            require_active(ctx.db, run.project_id, what='saving a workflow design')
            ctx.db.execute(text('select pg_advisory_xact_lock(hashtext(:key))'), {'key': f'workflow-design:{node.id}'})
            stop()
            call_rows = ai_svc.record_calls(ctx.db, calls, node.id)
            ctx.db.flush()
            prior = ctx.db.scalar(select(WorkflowDesignPlanVersion).where(WorkflowDesignPlanVersion.node_run_id == node.id))
            if prior is not None:
                _verify(ctx.db, ctx.user, prior, reference(prior))
                result = _outcome(ctx, prior)
                stop()
                return result
            row = WorkflowDesignPlanVersion(id=uuid.uuid4(), plan_id=plan_id, version=1, project_id=run.project_id,
                user_id=ctx.user.id, run_id=run.id, node_run_id=node.id, node_id=node.node_id, authoring_attempt=ctx.attempt,
                content=data, sha256=design_plans.digest(data), source_snapshot=source, source_sha256=design_plans.digest(source),
                provenance={'prompt_version': design_agent.PROMPT_VERSION, 'simulated': outcome.simulated,
                    'call_ids': [str(c.id) for c in call_rows], 'manufacturing_capabilities': capabilities,
                    'model': {k: model_ref.get(k) for k in ('config_id', 'config_version', 'provider', 'model_id')}},
                authorization={})
            row.authorization = _authorization(row, delegated=not waiting)
            ctx.db.add(row)
            ctx.db.flush()
            stop()
            result = _outcome(ctx, row)
            stop()
            return result
    except ValidationError as exc:
        raise NodeFailure('invalid_design_plan', 'the normalized design did not validate', calls=calls) from exc
    except AppError as exc:
        raise NodeFailure(exc.code, exc.message, details=exc.details, calls=calls) from exc
    except ProviderError as exc:
        raise NodeFailure(exc.code, exc.message, retryable=exc.retryable, calls=calls) from exc
