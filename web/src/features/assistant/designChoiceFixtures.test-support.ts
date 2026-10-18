// Frontend test/QA data only. These are not generated or saved engineering results.
import type { AiOperation, DesignChoiceQuestion, DesignChoiceReceipt, DesignPlanSummary } from '../../api/assistant';

export const designChoiceQuestion: DesignChoiceQuestion = {
  kind: 'design_choice', title: 'Choose fixture details', description: 'Adjust only the details that matter to your installation.',
  design_ref: { schema_version: 1, plan_id: '10000000-0000-4000-8000-000000000001', version_id: '10000000-0000-4000-8000-000000000002', version: 1, sha256: 'a'.repeat(64) },
  question_set_id: '10000000-0000-4000-8000-000000000003', can_answer: true,
  allowed_actions: ['customize', 'delegate_remaining', 'accept_recommended'], last_response: null,
  questions: [
    { id: 'width', key: 'width', label: 'Mounting width', description: 'Spacing between supports', input_kind: 'number', unit: 'mm', minimum: 0, maximum: 200,
      allow_custom: true, required: true, options: [{ id: 'medium', label: 'Medium width', value: 80, reason: 'Fits the design envelope' }], recommended: { kind: 'option', option_id: 'medium' } },
    { id: 'coat', key: 'coat', label: 'Protective coating', description: 'Leave open if you want the Agent to decide.', input_kind: 'boolean', unit: null, minimum: null, maximum: null,
      allow_custom: true, required: false, options: [], recommended: { kind: 'value', value: false } },
    { id: 'material', key: 'material', label: 'Support material', description: 'Choose from the planned materials or enter your own.', input_kind: 'string', unit: null, minimum: null, maximum: null,
      allow_custom: true, required: true, options: [{ id: 'steel', label: 'Steel', value: 'S235', reason: 'Stiff support' }, { id: 'aluminium', label: 'Aluminium', value: '6061-T6', reason: 'Lower weight' }], recommended: { kind: 'option', option_id: 'steel' } },
  ],
};

export const designChoiceReceipt: DesignChoiceReceipt = {
  client_request_id: '10000000-0000-4000-8000-000000000004', question_set_id: designChoiceQuestion.question_set_id,
  answered_design_ref: designChoiceQuestion.design_ref, design_ref: null, action: 'delegate_remaining',
  resume_operation_id: '10000000-0000-4000-8000-000000000005',
};

export const designChoicePlan: DesignPlanSummary = {
  ref: designChoiceQuestion.design_ref, title: 'Fixture design', summary: 'A reusable fixture with two supports.', status: 'awaiting_choice',
  structure: { root: 'fixture', definitions: 3, part_definitions: 2, assembly_definitions: 1, instances: 3 },
  decisions: [
    { id: 'width', key: 'width', label: 'Mounting width', value: 80, unit: 'mm', reason: 'Fits the envelope', origin: 'ai_design' },
    { id: 'material', key: 'material', label: 'Material', value: 'Steel', unit: null, reason: 'User requirement', origin: 'user_input' },
  ],
  questions: designChoiceQuestion.questions, last_response: null,
};

export const designChoiceOperation: AiOperation = {
  id: '10000000-0000-4000-8000-000000000006', conversation_id: '10000000-0000-4000-8000-000000000007', message_id: '10000000-0000-4000-8000-000000000008',
  kind: 'ask', status: 'waiting_input', generation: 1, completion_events: [], execution_policy: 'standard',
  request: { text: 'Design a fixture; I would like to choose some details.', operation: 'ask', requested_operation: 'auto', runtime: 'orchestrator', trigger: { type: 'request' },
    target: null, references: [], constraints: [], task_id: null, model_config_id: null, client_request_id: '10000000-0000-4000-8000-000000000009' },
  task: null, part: null, run: null, question: designChoiceQuestion,
  result: { kind: 'orchestrator', trigger: { type: 'request' }, simulated: true, design_plan: designChoicePlan, steps: [
    { index: 0, tool: 'plan_design', title: 'Plan the fixture', args: {}, status: 'waiting_design', message: '', refs: {}, started_at: '2026-10-18T00:00:00Z', finished_at: null },
  ] } as unknown as AiOperation['result'],
  error: null, diagnostic: null, simulated: true, retry_of: null, retried_by: null,
  created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z', finished_at: null, context_build: null,
};
