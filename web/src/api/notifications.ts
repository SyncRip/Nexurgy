// In-app notifications (contract .trellis/tasks/archive/2026-10/10-05-templates-stages/design.md
// §3.6, server/app/api/notifications.py). Rows are written by the outbox
// dispatcher; the client only lists them and marks them read.

import { request } from './client';
import type { ISODateTime, UUID } from './types';

type Opt = { signal?: AbortSignal };

/** request.opened / request.decided / request.reassigned / task.status / review.decided / impact.opened … */
export type NotificationKind = string;

export interface AppNotification {
  id: UUID;
  kind: NotificationKind;
  title: string;
  body: string;
  /** In-app path such as /approval-requests/<id> or /tasks/<id>; null = nothing to open. */
  link: string | null;
  project_id: UUID | null;
  project?: { id: UUID; code: string; name?: string } | null;
  /** The outbox event payload (request_kind, stage, title, decision, key, from, to, milestone_name, reason …). */
  payload?: Record<string, unknown>;
  created_at: ISODateTime | null;
  read_at: ISODateTime | null;
}

type ListBody = AppNotification[] | { items?: AppNotification[] };

export async function listNotifications(q: { unread?: boolean; limit?: number } = {}, o: Opt = {}): Promise<AppNotification[]> {
  const res = await request<ListBody>('/notifications', { query: { unread: q.unread ? 1 : undefined, limit: q.limit }, signal: o.signal });
  return Array.isArray(res) ? res : res?.items ?? [];
}

type CountBody = number | { count?: number; unread?: number; unread_count?: number };

export async function getUnreadNotificationCount(o: Opt = {}): Promise<number> {
  const res = await request<CountBody>('/notifications/unread-count', { signal: o.signal });
  if (typeof res === 'number') return res;
  return res?.count ?? res?.unread ?? res?.unread_count ?? 0;
}

/** Mark the given notifications (or all of them) read. Idempotent; returns the remaining unread count. */
export const markNotificationsRead = (body: { ids?: UUID[]; all?: boolean }, o: Opt = {}) =>
  request<{ updated: number; count: number }>('/notifications/read', { method: 'POST', body, signal: o.signal });
