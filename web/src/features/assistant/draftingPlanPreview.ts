import type { AiDraftingEditPlan } from '../../api/assistant';
import { hasKey } from '../../i18n/core';
import type { TFunction } from '../../i18n/I18nProvider';

type Command = AiDraftingEditPlan['commands'][number];
const fields: Record<string, string> = { text_override: 'override', start_angle: 'start', end_angle: 'end' };
const valueText = (value: unknown): string => typeof value === 'string' ? value : JSON.stringify(value) ?? '—';

/** Summaries use the validated executable values, not the model's prose about what it intended. */
export function draftingCommandPreview(t: TFunction, command: Command): string[] {
  const label = (key: string, fallback: string) => hasKey(key) ? t(key) : fallback;
  const values = (patch: Record<string, unknown>) => Object.entries(patch)
    .filter(([, value]) => value !== undefined)
    .map(([key, value]) => `${label(`dr.props.${fields[key] ?? key}`, label(`core.field.${key}`, key))} → ${valueText(value)}`).join('; ');
  const action = t(`core.command.${command.op}`);
  switch (command.op) {
    case 'update_entity': return [`${action} · ${command.id}: ${values(command.set)}`];
    case 'edit_text': return [`${action} · ${command.id}: ${values({ text: command.text, height: command.height })}`];
    case 'edit_dimension': return [`${action} · ${command.id}: ${t('dr.props.override')} → ${command.text_override ?? t('core.plan.measuredLabel')}`];
    case 'add_entities': return command.entities.map((entity) => {
      const { id, type, origin: _origin, source_ref: _source, modified: _modified, ...geometry } = entity;
      return `${action} · ${label(`dr.type.${type}`, type ?? '')}${id ? ` ${id}` : ''}: ${values(geometry)}`;
    });
    case 'delete_entities': return [`${action}: ${command.ids.join(', ')}`];
    case 'transform': return [`${action} · ${command.ids.join(', ')}: ${values({ dx: command.dx, dy: command.dy, rotate: command.rotate, center: command.center, copy: command.copy })}`];
    case 'set_entity_layer': return [`${action} · ${command.ids.join(', ')} → ${command.layer}`];
    case 'upsert_layer': return [`${action} · ${command.layer.name}: ${values({ ...command.layer })}`];
    case 'set_layer_visibility': return [`${action} · ${command.name} → ${t(command.visible ? 'core.plan.visible' : 'core.plan.hidden')}`];
    case 'define_block': return [`${action} · ${command.block.name}: ${t('core.plan.entityCount', { n: command.block.entities.length })}`];
    case 'insert_block': return [`${action} · ${command.block}: ${values({ insert: command.insert, rotation: command.rotation, xscale: command.xscale, yscale: command.yscale, layer: command.layer })}`];
    case 'set_title_block': return [`${action}: ${values(command.fields)}`];
    case 'set_bom': return [`${action}: ${t('core.plan.rowCount', { n: command.rows.length })}`];
    case 'set_sheet': return [`${action}: ${values(command.set)}`];
    case 'set_underlay': return [`${action} · ${command.id}: ${values(command.set)}`];
  }
}
