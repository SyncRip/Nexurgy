import { describe, expect, it } from 'vitest';
import { isCoordinatedOperation, roleForNode, roleForOperation, roleForTool } from './roles';

describe('engineering responsibility mapping', () => {
  it('keeps direct work and the corresponding coordinating tool in the same professional role', () => {
    for (const [operation, tool] of [
      ['model_create', 'create_model'], ['assembly_create', 'create_assembly'], ['assembly_modify', 'modify_assembly'],
      ['drawing_reconstruct', 'reconstruct_drawing'], ['spec_extract', 'extract_spec'],
      ['drafting_modify', 'modify_drawing_document'], ['workflow_create', 'create_workflow'],
    ]) {
      expect(roleForOperation(operation)).not.toBeNull();
      expect(roleForTool(tool)).toBe(roleForOperation(operation));
    }
    expect(roleForOperation('ask')).toBe('manager');
    expect(roleForTool('plan_design')).toBe('design');
    expect(roleForOperation('drawing_reconstruct')).toBe('modeling');
  });

  it('separates complete assembly workflow coordination from specialist work', () => {
    expect(isCoordinatedOperation('assembly_create')).toBe(true);
    expect(roleForOperation('assembly_create')).toBe('manager');
    expect(roleForTool('create_assembly')).toBe('manager');
    expect(roleForOperation('assembly_modify')).toBe('assembly');
    expect(roleForOperation('model_create')).toBe('modeling');
    for (const [node, role] of [['engineering_spec', 'design'], ['assembly_from_spec', 'assembly'],
      ['model_3d', 'modeling'], ['assembly_drawing', 'drawing'], ['delivery_package', 'manager']]) {
      expect(roleForNode(node)).toBe(role);
      expect(isCoordinatedOperation(node)).toBe(false);
    }
    for (const direct of ['assembly_modify', 'model_create', 'spec_extract', 'drawing_generate', 'ask']) {
      expect(isCoordinatedOperation(direct)).toBe(false);
    }
  });

  it('recognizes the actual part and assembly manufacturing stages without inventing process operations', () => {
    expect(roleForNode('process_plan')).toBe('process');
    expect(roleForNode('assembly_process_plan')).toBe('process');
    expect(roleForNode('quote')).toBe('quote');
    expect(roleForNode('assembly_quote')).toBe('quote');
    expect(roleForTool('create_process_plan')).toBe(roleForNode('process_plan'));
    expect(roleForTool('create_quote')).toBe(roleForNode('quote'));
    expect(roleForNode('assembly_from_spec')).toBe('assembly');
    expect(roleForNode('assembly_drawing')).toBe('drawing');
    expect(roleForNode('manufacturing_export')).toBe('manager');
    expect(roleForOperation('process_plan')).toBeNull();
    expect(roleForTool('assembly_quote')).toBeNull();
  });

  it('leaves human approvals, inputs, custom tasks and unknown identifiers unassigned', () => {
    for (const value of ['human_review', 'approval', 'business_task', 'text_input', 'model_input', 'custom_3d_engineer', '未来报价工程师', '__proto__', 'toString', '', null, undefined]) {
      expect(roleForNode(value)).toBeNull();
      expect(roleForOperation(value)).toBeNull();
      expect(roleForTool(value)).toBeNull();
      expect(isCoordinatedOperation(value)).toBe(false);
    }
  });
});
