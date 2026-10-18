// The tool contract's parameter rows (agent execution layer P2): the top-level properties of a tool's args_schema
// (pydantic's JSON Schema, exactly what the model is given) as name / type / required / description.

import type { JsonSchema } from '../../api/agents';

export interface ToolParam {
  name: string;
  /** Short type label: string, int, number, bool, UUID, enum values (a | b), X[] for lists, a $defs title for objects. */
  type: string;
  required: boolean;
  description: string;
  /** The schema default when the parameter is optional and has a meaningful one (not null / empty). */
  defaultValue?: string;
}

const SCALAR: Record<string, string> = { string: 'string', integer: 'int', number: 'number', boolean: 'bool', object: 'object', array: 'list' };

function refTitle(ref: string, defs: Record<string, JsonSchema> | undefined): string {
  const key = ref.split('/').pop() ?? ref;
  return defs?.[key]?.title ?? key;
}

/** One schema's type label; nullable branches are left out (the "required" column says whether a value is needed). */
export function typeLabel(s: JsonSchema | undefined, defs?: Record<string, JsonSchema>): string {
  if (!s) return 'any';
  if (s.$ref) return refTitle(s.$ref, defs);
  const variants = s.anyOf ?? s.oneOf;
  if (variants) {
    const labels = variants.filter((v) => v.type !== 'null').map((v) => typeLabel(v, defs));
    return [...new Set(labels)].join(' | ') || 'null';
  }
  if (s.enum) return s.enum.map((v) => String(v)).join(' | ');
  if (s.const !== undefined) return String(s.const);
  const type = Array.isArray(s.type) ? s.type.filter((x) => x !== 'null')[0] : s.type;
  if (type === 'array') {
    const inner = typeLabel(s.items, defs);
    return inner.includes(' | ') ? `(${inner})[]` : `${inner}[]`;
  }
  if (type === 'string' && s.format === 'uuid') return 'UUID';
  return (type && SCALAR[type]) ?? type ?? 'any';
}

function shownDefault(v: unknown): string | undefined {
  if (v === undefined || v === null || v === '' || (Array.isArray(v) && v.length === 0)) return undefined;
  return typeof v === 'string' ? v : JSON.stringify(v);
}

/** The parameter rows of a tool's argument schema, in schema order. */
export function schemaParams(schema: JsonSchema | undefined): ToolParam[] {
  const props = schema?.properties ?? {};
  const required = new Set(schema?.required ?? []);
  return Object.entries(props).map(([name, s]) => {
    const isRequired = required.has(name);
    return {
      name,
      type: typeLabel(s, schema?.$defs),
      required: isRequired,
      description: s.description ?? '',
      defaultValue: isRequired ? undefined : shownDefault(s.default),
    };
  });
}
