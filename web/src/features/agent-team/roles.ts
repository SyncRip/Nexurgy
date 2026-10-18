import type { AiOperationKind } from '../../api/assistant';

/** Professional functions of the AI, independent of execution state or a user's custom agent profiles. */
export const ENGINEERING_ROLES = ['manager', 'design', 'modeling', 'assembly', 'drawing', 'process', 'quote'] as const;
export type EngineeringRole = (typeof ENGINEERING_ROLES)[number];

const OPERATIONS = {
  ask: 'manager',
  task_draft: 'manager',
  workflow_create: 'manager',
  workflow_edit: 'manager',
  spec_extract: 'design',
  model_create: 'modeling',
  model_modify: 'modeling',
  drawing_reconstruct: 'modeling',
  // Starts the complete specification → assembly → drawings → delivery workflow.
  assembly_create: 'manager',
  assembly_modify: 'assembly',
  drawing_intake: 'drawing',
  drawing_generate: 'drawing',
  drafting_create: 'drawing',
  drafting_modify: 'drawing',
} as const satisfies Record<AiOperationKind, EngineeringRole>;

const TOOLS: Readonly<Record<string, EngineeringRole>> = {
  plan_design: 'design',
  draft_task: OPERATIONS.task_draft,
  update_task_draft: OPERATIONS.task_draft,
  create_part: 'manager',
  create_task: 'manager',
  create_model: OPERATIONS.model_create,
  modify_model: OPERATIONS.model_modify,
  generate_drawing: OPERATIONS.drawing_generate,
  create_process_plan: 'process',
  create_quote: 'quote',
  create_assembly: OPERATIONS.assembly_create,
  modify_assembly: OPERATIONS.assembly_modify,
  create_workflow: OPERATIONS.workflow_create,
  edit_workflow: OPERATIONS.workflow_edit,
  read_drawing: OPERATIONS.drawing_intake,
  reconstruct_drawing: OPERATIONS.drawing_reconstruct,
  extract_spec: OPERATIONS.spec_extract,
  create_drawing_document: OPERATIONS.drafting_create,
  modify_drawing_document: OPERATIONS.drafting_modify,
  get_operation_status: 'manager',
};

// Only registered engineering stages have a role. Inputs, human review and custom business tasks do not.
const NODES: Readonly<Record<string, EngineeringRole>> = {
  concept_design: 'design',
  engineering_spec: 'design',
  model_3d: 'modeling',
  drawing_reconstruct: 'modeling',
  assembly_from_spec: 'assembly',
  drawing: 'drawing',
  drawing_intake: 'drawing',
  assembly_drawing: 'drawing',
  process_plan: 'process',
  assembly_process_plan: 'process',
  quote: 'quote',
  assembly_quote: 'quote',
  export: 'manager',
  delivery_package: 'manager',
  manufacturing_export: 'manager',
};

function roleFrom(table: Readonly<Record<string, EngineeringRole>>, value: string | null | undefined): EngineeringRole | null {
  return value && Object.hasOwn(table, value) ? table[value] : null;
}

export const roleForOperation = (kind: string | null | undefined): EngineeringRole | null => roleFrom(OPERATIONS, kind);
export const roleForTool = (tool: string | null | undefined): EngineeringRole | null => roleFrom(TOOLS, tool);
export const roleForNode = (nodeType: string | null | undefined): EngineeringRole | null => roleFrom(NODES, nodeType);

/** This saved operation coordinates several professions; its stages retain their own roles. */
export const isCoordinatedOperation = (kind: string | null | undefined): boolean => kind === 'assembly_create';
