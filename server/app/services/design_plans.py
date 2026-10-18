"""Immutable design authority and exact-question responses for the main Agent."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..ai import retry as call_retry
from ..ai.design_contracts import DesignChoiceIn, DesignDecision, DesignPlanContent, DesignQuestion, DesignRef
from ..errors import AppError, conflict
from ..models import AiDesignChoice, AiDesignPlanVersion, AiMessage, AiOperation, User
from ..security import project_access
from .assistant_tool_inputs import ResolvedDesignPlan

STATE = 'design_plan_state'
TOOLS = ('assembly_create', 'spec_extract', 'modeling', 'assembly_from_spec')
PART_TOOLS = ('model_create', 'spec_extract', 'modeling')
MAX_PLAN_BYTES = 180_000


def digest(value: Any) -> str:
    """Exact canonical JSON, without rounding away any authored numeric value."""
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def reference(row: AiDesignPlanVersion) -> DesignRef:
    return DesignRef(plan_id=row.plan_id, version_id=row.id, version=row.version, sha256=row.sha256)


def question_set_id(row: AiDesignPlanVersion) -> str:
    return str(uuid.uuid5(row.id, 'design-questions'))


def _state(root: AiOperation) -> dict:
    return (root.result or {}).get(STATE) or {}


def _set_state(root: AiOperation, **values: Any) -> None:
    root.result = {**(root.result or {}), STATE: {**_state(root), **values}}


def _root(db: Session, op: AiOperation) -> AiOperation:
    from .orchestrator import root_of
    root = db.get(AiOperation, root_of(op), populate_existing=True)
    if root is None or root.conversation_id != op.conversation_id or root.user_id != op.user_id:
        raise conflict('design_scope_mismatch', 'the design root does not belong to this operation')
    return root


def _superseded(db: Session, root: AiOperation) -> bool:
    """A newer human root invalidates an unresolved choice even if SKIP LOCKED missed its row."""
    state = _state(root)
    if not (state.get('waiting_operation_id') or state.get('pending_choice_id')):
        return False
    original = db.get(AiMessage, root.message_id)
    if original is None:
        return True
    later = select(AiOperation.id).join(AiMessage, AiOperation.message_id == AiMessage.id).where(
        AiOperation.conversation_id == root.conversation_id,
        AiMessage.role == 'user', AiMessage.kind == 'request', AiMessage.seq > original.seq,
        or_(AiOperation.request['root_op_id'].astext.is_(None),
            AiOperation.request['root_op_id'].astext != str(root.id)),
    ).limit(1)
    return db.scalar(later) is not None


def _live(db: Session, user: User, root: AiOperation) -> None:
    from . import assistant as asst, orchestrator
    from .lifecycle import require_active
    _, conv, _ = asst.load_operation(db, user, root.id)
    project_access(db, user, root.project_id, 'design')
    require_active(db, root.project_id, what='using a design plan')
    asst.require_open(conv)
    orchestrator.require_enabled(root)
    if root.status == 'cancelled' or _state(root).get('closed') or _superseded(db, root):
        raise conflict('design_plan_cancelled', 'this design request was cancelled or superseded')
    if (root.request or {}).get('read_only'):
        raise conflict('design_not_authorized', 'a read-only request cannot authorize a design')


def _row(db: Session, ref: DesignRef) -> AiDesignPlanVersion:
    row = db.get(AiDesignPlanVersion, ref.version_id)
    if row is None or row.plan_id != ref.plan_id or row.version != ref.version:
        raise conflict('design_scope_mismatch', 'the exact design version does not exist in this scope')
    if row.sha256 != ref.sha256 or digest(row.content) != ref.sha256 or digest(row.source_snapshot) != row.source_sha256:
        raise conflict('design_hash_mismatch', 'the design or source digest does not match the immutable reference')
    return row


def resolve_authorized_design(db: Session, user: User, design_ref: DesignRef | dict, *, conversation_id: uuid.UUID,
                              root_operation_id: uuid.UUID, tool_kind: str) -> ResolvedDesignPlan:
    from . import assistant as asst
    try:
        ref = design_ref if isinstance(design_ref, DesignRef) else DesignRef.model_validate(design_ref)
    except (ValidationError, TypeError) as exc:
        raise conflict('design_scope_mismatch', 'the design reference is invalid') from exc
    root, _, _ = asst.load_operation(db, user, root_operation_id)
    _live(db, user, root)
    row = _row(db, ref)
    if (root.conversation_id != conversation_id or row.conversation_id != conversation_id or
            row.root_operation_id != root.id or row.project_id != root.project_id or row.user_id != user.id):
        raise conflict('design_scope_mismatch', 'the design belongs to a different owner, project or human root')
    state = _state(root)
    if state.get('current_ref') != ref.model_dump(mode='json'):
        raise conflict('design_stale', 'this design was superseded; an execution cannot rebase to latest implicitly')
    if (state.get('waiting_operation_id') or state.get('pending_choice_id') or
            tool_kind not in row.authorization.get('tools', []) or not row.authorization.get('delegated')):
        raise conflict('design_not_authorized', 'this exact design still requires a bound user choice')
    snapshot = row.source_snapshot
    if (snapshot.get('text') != str((root.request or {}).get('text') or '') or
            snapshot.get('operation_id') != str(root.id) or snapshot.get('message_id') != str(root.message_id)):
        raise conflict('design_hash_mismatch', 'the immutable original human source no longer matches its root')
    return ResolvedDesignPlan(ref=ref, content=DesignPlanContent.model_validate(row.content),
        source_snapshot=copy.deepcopy(snapshot), source_sha256=row.source_sha256,
        authorization=copy.deepcopy(row.authorization))


def selected_ref(db: Session, user: User, turn: AiOperation, *, tool_kind: str) -> DesignRef | None:
    root = _root(db, turn)
    current = _state(root).get('current_ref')
    if not current:
        return None
    return resolve_authorized_design(db, user, current, conversation_id=turn.conversation_id,
                                     root_operation_id=root.id, tool_kind=tool_kind).ref


def _value(content: dict, path: list[str | int]) -> Any:
    current: Any = content
    try:
        for key in path:
            if isinstance(current, list) and (type(key) is not int or key < 0):
                raise KeyError(key)
            current = current[key]
    except (KeyError, IndexError, TypeError) as exc:
        raise AppError(422, 'design_answer_invalid', 'a design field no longer resolves') from exc
    return current


def _questions(content: DesignPlanContent) -> None:
    data = content.model_dump(mode='json')
    seen, ids = set(), set()
    forbidden = {'id', 'key', 'kind', 'definition', 'parent', 'ref', 'source', 'evidence', 'origin', 'fact_ids'}
    for q in content.questions:
        details = {'question_key': q.key, 'field_path': q.field_path, 'input_kind': q.input_kind}
        if q.key in seen or q.id in ids or q.field_path[0] not in {'definitions', 'instances', 'relations', 'facts', 'interfaces', 'part_requirements'}:
            raise AppError(422, 'invalid_design_plan', 'design questions must uniquely address actual design parameters', details)
        seen.add(q.key)
        ids.add(q.id)
        if any(key in forbidden for key in q.field_path if isinstance(key, str)):
            raise AppError(422, 'invalid_design_plan', 'a question cannot edit graph identity or evidence', details)
        _check_value(q, _value(data, q.field_path))
        for option in q.options:
            _check_value(q, option.value)
        if q.recommended and q.recommended.kind == 'value':
            _check_value(q, q.recommended.value)


def _check_value(question: DesignQuestion, value: Any) -> None:
    valid = {'string': isinstance(value, str), 'number': type(value) in (int, float) and math.isfinite(value),
             'boolean': type(value) is bool}[question.input_kind]
    if not valid or isinstance(value, str) and len(value) > 1000:
        raise AppError(422, 'design_answer_invalid', 'the answer has the wrong scalar type or is too long',
            {'question_key': question.key, 'field_path': question.field_path, 'input_kind': question.input_kind,
             'actual_type': type(value).__name__})
    if question.input_kind == 'number' and ((question.minimum is not None and value < question.minimum) or
                                            (question.maximum is not None and value > question.maximum)):
        raise AppError(422, 'design_answer_invalid', 'the answer is outside this question\'s bounds',
            {'question_key': question.key, 'field_path': question.field_path,
             'minimum': question.minimum, 'maximum': question.maximum})


def validate_planner_content(content: DesignPlanContent, source: dict[str, Any]) -> DesignPlanContent:
    """Validate executable geometry and its editable fields inside the same bounded planner repair loop."""
    from .design_spec import validate_plan_content

    plan = validate_plan_content(content, source)
    _questions(plan)
    return plan


def receipt(db: Session, choice: AiDesignChoice) -> dict:
    saved = db.scalar(select(AiDesignPlanVersion).where(AiDesignPlanVersion.choice_id == choice.id))
    body = choice.request
    return {'client_request_id': str(choice.client_request_id), 'question_set_id': body['question_set_id'],
            'answered_design_ref': body['design_ref'], 'design_ref': reference(saved).model_dump(mode='json') if saved else None,
            'action': body['action'], 'resume_operation_id': str(choice.resume_operation_id)}


def summary(db: Session, row: AiDesignPlanVersion, *, waiting: bool = False, closed: bool = False,
            response: AiDesignChoice | None = None) -> dict:
    data = row.content
    root = next(definition for definition in data['definitions'] if definition['key'] == data['root'])
    return {'ref': reference(row).model_dump(mode='json'), 'title': data['title'], 'summary': data.get('summary') or '',
        'root_kind': root['kind'], 'root_name': root['name'],
        'status': 'superseded' if closed else 'awaiting_choice' if waiting else 'adopted',
        'structure': {'root': data['root'], 'definitions': len(data['definitions']),
            'part_definitions': sum(d['kind'] != 'assembly' for d in data['definitions']),
            'assembly_definitions': sum(d['kind'] == 'assembly' for d in data['definitions']),
            'instances': len(data['instances'])},
        'decisions': data.get('decisions') or [],
        'questions': [{k: v for k, v in q.items() if k != 'field_path'} for q in data.get('questions') or []],
        'last_response': receipt(db, response) if response else None}


def question(row: AiDesignPlanVersion) -> dict:
    return {'kind': 'design_choice', 'design_ref': reference(row).model_dump(mode='json'),
        'question_set_id': question_set_id(row), 'title': row.content['title'],
        'description': row.content.get('summary') or '',
        'questions': [{k: v for k, v in q.items() if k != 'field_path'} for q in row.content.get('questions') or []],
        'can_answer': True, 'allowed_actions': ['customize', 'delegate_remaining', 'accept_recommended'], 'last_response': None}


def project_operation(db: Session, op: AiOperation, result: Any) -> tuple[Any, Any]:
    """Read-only recovery of current permission and async response receipts."""
    q = copy.deepcopy(op.question)
    if not isinstance(result, dict) or not isinstance(result.get('design_plan'), dict):
        return result, q
    result = copy.deepcopy(result)
    root = _root(db, op)
    state = _state(root)
    response_id = (result.get('design_plan') or {}).get('response_id')
    response = db.get(AiDesignChoice, uuid.UUID(response_id)) if response_id else None
    if response:
        result['design_plan']['last_response'] = receipt(db, response)
    result['design_plan'].pop('response_id', None)
    result.pop(STATE, None)
    superseded = state.get('closed') or root.status == 'cancelled' or _superseded(db, root)
    if superseded:
        result['design_plan']['status'] = 'superseded'
    if isinstance(q, dict) and q.get('kind') == 'design_choice':
        can_answer = (op.status == 'waiting_input' and state.get('waiting_operation_id') == str(op.id)
                      and not state.get('pending_choice_id') and not superseded)
        if can_answer:
            try:
                _live(db, db.get(User, op.user_id), root)
            except AppError:
                can_answer = False
        q.update(can_answer=can_answer, allowed_actions=q['allowed_actions'] if can_answer else [])
    return result, q


def _snapshot(root: AiOperation, prior: AiDesignPlanVersion | None, choice: AiDesignChoice | None) -> dict:
    req = root.request or {}
    data = copy.deepcopy(prior.source_snapshot if prior else {
        'operation_id': str(root.id), 'message_id': str(root.message_id), 'text': str(req.get('text') or ''),
        'materials': req.get('materials') or [], 'constraints': req.get('constraints') or [],
        'parameter_overrides': [], 'user_responses': []})
    if choice:
        overrides = {json.dumps(v['field_path']): v for v in data['parameter_overrides']}
        overrides.update({json.dumps(v['field_path']): v for v in choice.parameter_overrides})
        data['parameter_overrides'] = list(overrides.values())
        data['user_responses'].append({'choice_id': str(choice.id), 'client_request_id': str(choice.client_request_id),
            'action': choice.request['action'], 'text': choice.request.get('free_text') or '',
            'answers': choice.request.get('answers') or []})
        if prior:
            from .design_spec import effective_parameter_overrides
            data['parameter_overrides'] = effective_parameter_overrides(prior.content, data)
    return data


def _response_turn(db: Session, turn: AiOperation, choice: AiDesignChoice) -> bool:
    """Only the persisted continuation and its explicit human Retry chain may consume this answer."""
    seen = set()
    current = turn
    while current.id not in seen and len(seen) < 64:
        seen.add(current.id)
        if current.id == choice.resume_operation_id:
            return True
        if current.retry_of is None:
            return False
        previous = db.get(AiOperation, current.retry_of)
        if previous is None or previous.conversation_id != turn.conversation_id or previous.user_id != turn.user_id:
            return False
        current = previous
    return False


def plan(db: Session, user: User, turn: AiOperation) -> tuple[dict, dict | None]:
    """Prepare outside locks, recheck and append once. Only a stored human response can revise a plan."""
    from ..ai import design_agent
    from . import ai as ai_svc, assistant as asst, design_spec
    from .assistant_cad_tools import stop_guard
    root = _root(db, turn)
    _live(db, user, root)
    if turn.status != 'running':
        raise conflict('turn_stopped', 'the main-agent turn is no longer running')
    root_req = root.request or {}
    if root_req.get('target') or root_req.get('references') or root_req.get('materials'):
        raise conflict('design_not_authorized', 'referenced source reconstruction keeps its existing extraction path')
    state = _state(root)
    prior = _row(db, DesignRef.model_validate(state['current_ref'])) if state.get('current_ref') else None
    trigger = (turn.request or {}).get('trigger') or {}
    choice = None
    if state.get('pending_choice_id'):
        if trigger.get('type') != 'design_choice' or trigger.get('choice_id') != state['pending_choice_id']:
            raise conflict('design_stale', 'a bound design response is being processed by its own turn')
        choice = db.get(AiDesignChoice, uuid.UUID(state['pending_choice_id']))
        if choice is None or not _response_turn(db, turn, choice) or choice.root_operation_id != root.id:
            raise conflict('design_scope_mismatch', 'the response is not bound to this continuation')
    if prior and not choice:
        waiting = bool(state.get('waiting_operation_id'))
        return summary(db, prior, waiting=waiting), question(prior) if waiting else None
    # No prior version: only the human request turn may start planning, never a completion wake.
    if prior is None and trigger.get('type') != 'request':
        raise conflict('design_not_authorized', 'completion events cannot create a new design plan')
    source = _snapshot(root, prior, choice)
    model_ref = ai_svc.bundle_model_ref(root_req.get('model_snapshots'), 'planning')
    provider, _ = ai_svc.provider_for(db, 'planning', model_ref)
    payload = {'source': source, 'previous_plan': prior.content if prior else None,
               'response': choice.request if choice else None, 'execution_policy': root.execution_policy}
    root_id, turn_id = root.id, turn.id
    prior_id, choice_id = prior.id if prior else None, choice.id if choice else None
    local_deadline = time.monotonic() + 300
    live_stop = stop_guard(turn, timeout_s=300)
    db.commit()
    with call_retry.deadline_scope(local_deadline, check=live_stop) as call_limits:
        def stop() -> None:
            reason = call_limits.stop_reason(calls=False)
            remaining = call_limits.remaining_s()
            # The five-second reserve blocks NEW requests, not an already completed reply/save within its deadline.
            if reason is not None and (reason.kind in ('cancelled', 'lease_lost') or remaining is not None and remaining <= 0):
                raise reason.error()

        outcome = design_agent.run_design(provider, payload, stop=stop, root_mode='part_or_assembly',
                                         validate=lambda content: validate_planner_content(content, source))
    call_rows = ai_svc.record_calls(db, outcome.calls, None, ai_operation_id=turn_id)
    db.flush()
    call_ids = [str(row.id) for row in call_rows]
    db.commit()  # Failed or cancelled planning retains its call ledger as well.
    stop()
    turn = asst._lock_op(db, turn_id)
    conv = asst.lock_conversation(db, turn.conversation_id)
    asst.require_open(conv)
    root = db.get(AiOperation, root_id, populate_existing=True)
    _live(db, user, root)
    if turn.status != 'running':
        raise conflict('turn_stopped', 'the planning turn was stopped before saving')
    state = _state(root)
    current_id = (state.get('current_ref') or {}).get('version_id')
    if current_id != (str(prior_id) if prior_id else None) or state.get('pending_choice_id') != (str(choice_id) if choice_id else None):
        # A concurrent duplicate tool call can adopt the exact first result, never mint another version.
        if current_id and not state.get('pending_choice_id'):
            saved = _row(db, DesignRef.model_validate(state['current_ref']))
            return summary(db, saved, waiting=bool(state.get('waiting_operation_id'))), question(saved) if state.get('waiting_operation_id') else None
        raise conflict('design_stale', 'the design changed during planning')
    if outcome.error or outcome.reply is None:
        err = outcome.error or {'code': 'invalid_design_plan', 'message': 'no valid design was returned'}
        raise AppError(422, err['code'], err['message'], err.get('details') or {})
    content = outcome.reply.content
    # The authoring model never gets to label its own choice as human-confirmed.
    for decision in content.decisions:
        decision.origin = 'ai_design'
    # Displayed human-origin decisions come from validated source constraints and immutable actual answers.
    decisions = {decision.key: decision for decision in content.decisions}
    for constraint in content.hard_constraints:
        key = 'user:' + constraint.id
        decisions[key] = DesignDecision(id=str(uuid.uuid5(root.id, 'decision:' + key)), key=key, label=constraint.label, value=constraint.value,
            unit=constraint.unit, reason=constraint.source_quote, origin='user_input')
    for override in source['parameter_overrides']:
        key = override['key']
        old = decisions.get(key)
        decisions[key] = DesignDecision(id=old.id if old else str(uuid.uuid5(root.id, 'decision:' + key)), key=key,
            label=override.get('label') or (old.label if old else key), value=override['value'], unit=override.get('unit'),
            reason='Explicit user choice', origin='user_choice')
    content.decisions = list(decisions.values())
    waiting = outcome.reply.wait_for_user
    if choice and choice.request['action'] == 'customize' and prior:
        # A partial edit cannot erase an unanswered required choice. Actual prior answers remain frozen overrides.
        answered = {v['key'] for v in source['parameter_overrides']}
        old_plan = DesignPlanContent.model_validate(prior.content)
        current = content.model_dump(mode='json')
        constrained_paths = {tuple(c.field_path) for c in content.hard_constraints}
        answered.update(q.key for q in old_plan.questions if tuple(q.field_path) in constrained_paths
            and design_spec.semantic_anchor(current, q.field_path) == design_spec.semantic_anchor(prior.content, q.field_path))
        remaining = [q for q in old_plan.questions if q.required and q.key not in answered]
        if remaining:
            for q in remaining:
                if design_spec.semantic_anchor(current, q.field_path) != design_spec.semantic_anchor(prior.content, q.field_path):
                    raise AppError(422, 'design_answer_invalid', 'an unanswered required choice changed its semantic target')
            by_key = {q.key: q for q in content.questions}
            for q in remaining:
                by_key[q.key] = q.model_copy(deep=True)
            content.questions = list(by_key.values())
            waiting = True
    version = (prior.version if prior else 0) + 1
    plan_id = uuid.uuid5(root.id, 'design-plan')
    for q in content.questions:
        q.id = str(uuid.uuid5(plan_id, f'{version}:{q.key}'))
    _questions(content)
    if waiting and not content.questions:
        raise AppError(422, 'invalid_design_plan', 'a customization wait needs actual design questions')
    normalized = design_spec.validate_plan_content(content, source)
    data = normalized.model_dump(mode='json')
    root_definition = next(definition for definition in normalized.definitions if definition.key == normalized.root)
    tools = TOOLS if root_definition.kind == 'assembly' else PART_TOOLS
    if len(json.dumps(data, ensure_ascii=False).encode()) > MAX_PLAN_BYTES:
        raise AppError(422, 'context_over_budget', 'the design plan is too large to persist or execute')
    stop()  # Final validation/normalization cannot adopt a plan after cancellation or the same enclosing deadline.
    row = AiDesignPlanVersion(id=uuid.uuid4(), plan_id=plan_id, version=version, parent_version_id=prior_id,
        project_id=root.project_id, conversation_id=root.conversation_id, user_id=user.id,
        root_operation_id=root.id, authoring_operation_id=turn.id, choice_id=choice_id,
        content=data, sha256=digest(data), source_snapshot=source, source_sha256=digest(source),
        provenance={'prompt_version': design_agent.PROMPT_VERSION, 'simulated': outcome.simulated, 'call_ids': call_ids,
            'model': {k: model_ref.get(k) for k in ('config_id', 'config_version', 'provider', 'model_id')} if isinstance(model_ref, dict) else None},
        authorization={'kind': 'user_response' if choice else 'new_design_request', 'root_operation_id': str(root.id),
            'user_id': str(user.id), 'tools': list(tools), 'delegated': not waiting,
            'choice_id': str(choice_id) if choice_id else None})
    db.add(row)
    db.flush()
    projection = summary(db, row, waiting=waiting, response=choice)
    if choice:
        projection['response_id'] = str(choice.id)
    _set_state(root, current_ref=reference(row).model_dump(mode='json'),
               waiting_operation_id=str(turn.id) if waiting else None, pending_choice_id=None)
    root.result = {**(root.result or {}), 'design_plan': projection}
    turn.result = {**(turn.result or {}), 'design_plan': projection}
    db.commit()
    return projection, question(row) if waiting else None


def respond(db: Session, user: User, operation_id: uuid.UUID, body: DesignChoiceIn) -> tuple[AiOperation, AiDesignChoice]:
    from . import assistant as asst, orchestrator
    from .design_spec import semantic_anchor
    op, _, _ = asst.load_operation(db, user, operation_id)
    # Same operation -> conversation lock order as confirm/cancel. SKIP LOCKED supersession never inverts it.
    op = asst._lock_op(db, op.id)
    conv = asst.lock_conversation(db, op.conversation_id)
    root = _root(db, op)
    _live(db, user, root)
    raw = body.model_dump(mode='json')
    fingerprint = digest(raw)
    found = db.scalar(select(AiDesignChoice).where(AiDesignChoice.root_operation_id == root.id,
                                                   AiDesignChoice.client_request_id == body.client_request_id))
    if found:
        if found.request_sha256 != fingerprint or found.operation_id != op.id:
            raise conflict('design_idempotency_conflict', 'this request ID already belongs to a different design response')
        db.commit()
        return op, found
    q = op.question or {}
    if op.status != 'waiting_input' or q.get('kind') != 'design_choice':
        raise conflict('design_not_waiting', 'this operation is not waiting for design choices')
    if not q.get('can_answer') or body.action not in (q.get('allowed_actions') or []):
        raise conflict('design_not_authorized', 'this action is not allowed for the stored design question')
    if q.get('design_ref') != raw['design_ref'] or q.get('question_set_id') != str(body.question_set_id):
        raise conflict('design_stale', 'the response does not identify the displayed immutable question set')
    state = _state(root)
    if state.get('waiting_operation_id') != str(op.id) or state.get('current_ref') != raw['design_ref'] or state.get('pending_choice_id'):
        raise conflict('design_stale', 'this question set was already superseded or answered')
    row = _row(db, body.design_ref)
    if row.root_operation_id != root.id or row.user_id != user.id or row.conversation_id != conv.id:
        raise conflict('design_scope_mismatch', 'this plan is outside the authenticated operation scope')
    questions = {v.id: v for v in DesignPlanContent.model_validate(row.content).questions}
    overrides, lines = [], []
    for answer in body.answers:
        item = questions.get(answer.question_id)
        if item is None:
            raise AppError(422, 'design_answer_invalid', 'the answer names an unknown question')
        if answer.kind == 'option':
            option = next((o for o in item.options if o.id == answer.option_id), None)
            if option is None:
                raise AppError(422, 'design_answer_invalid', 'the answer names an unknown option')
            value = option.value
        else:
            if not item.allow_custom:
                raise AppError(422, 'design_answer_invalid', 'this question only accepts its listed options')
            value = answer.value
        _check_value(item, value)
        overrides.append({'field_path': item.field_path, 'value': value, 'unit': item.unit,
                          'question_id': item.id, 'key': item.key, 'label': item.label,
                          'semantic_anchor': semantic_anchor(row.content, item.field_path)})
        lines.append(f'{item.label}: {value}' + (f' {item.unit}' if item.unit else ''))
    choice_id = uuid.uuid5(root.id, f'design-response:{body.client_request_id}')
    trigger = {'type': 'design_choice', 'operation_id': str(op.id), 'choice_id': str(choice_id),
               'question_set_id': str(body.question_set_id), 'answered_design_ref': raw['design_ref']}
    wake = orchestrator._queue_turn(db, conv, root, trigger, uuid.uuid5(choice_id, 'resume'))
    choice = AiDesignChoice(id=choice_id, root_operation_id=root.id, operation_id=op.id, user_id=user.id,
        plan_version_id=row.id, client_request_id=body.client_request_id, request=raw,
        request_sha256=fingerprint, parameter_overrides=overrides, resume_operation_id=wake.id)
    db.add(choice)
    db.flush()
    projection = summary(db, row, waiting=True, response=choice)
    projection['response_id'] = str(choice.id)
    op.result = {**(op.result or {}), 'design_plan': projection}
    _set_state(root, pending_choice_id=str(choice.id), waiting_operation_id=None)
    orchestrator._close_waiting(op, 'ok')
    op.status, op.question = 'succeeded', None
    action_labels = {'customize': '调整方案', 'delegate_remaining': '其余由 AI 决定', 'accept_recommended': '采用推荐方案'}
    message = '\n'.join([action_labels[body.action], *lines, *([body.free_text] if body.free_text else [])])
    asst._append(db, conv, role='user', kind='request', text=message, operation_id=op.id,
        payload={'event': 'design_choice', 'receipt': receipt(db, choice), 'answers': raw['answers'], 'free_text': body.free_text})
    asst._on_terminal(db, op)
    db.commit()
    return op, choice


def close(op: AiOperation) -> None:
    """Supersession/cancellation closes the durable execution latch, never its history."""
    if _state(op):
        _set_state(op, closed=True, waiting_operation_id=None, pending_choice_id=None)
