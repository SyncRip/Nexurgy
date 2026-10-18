import { runChoices } from './capabilities';
// One function per API endpoint (design.md §6 plus the extra routes the server
// exposes: intakes, approvals list, plan/quote getters, workflow archive).
// Paths are relative to /api/v1 unless noted. Every function maps to a route
// that exists in server/app/api/*.py; a 404 ApiError from a missing route or
// resource has code "not_found".

import { ApiError,buildUrl,currentToken,isAbortError,parseErrorBody,request,toFormData } from './client';
import type {
AiCallRecord,
Approval,
ApprovalCreate,
ApprovalCreated,
CadPreviewRequest,
CadPreviewResponse,
Capabilities,
Drawing,
DrawingConfig,
FileListQuery,
FilePatch,
FileUpload,
FileVersionUpload,
Healthz,
ImportTemplateRequest,
Intake,
LoginRequest,
LoginResponse,
ManualIntakeCreate,
Member,
MemberUpsert,
ModelConfig,
ModelConfigCreate,
ModelConfigPatch,
ModelListRequest,
ModelListResult,
ModelRevision,
NodeDecision,
Part,
PartCopyRequest,
PartCreate,
PartListQuery,
PartPatch,
PartPipelineStatus,
ProcessPlan,
ProcessPlanCreate,
Project,
ProjectCreate,
ProjectFile,
ProjectListQuery,
ProjectPatch,
PublicTemplate,
PublicTemplateDetail,
PublicTemplateVersion,
PublishRequest,
Quote,
QuoteCreate,
RerunRequest,
RevisionCreate,
RevisionDiff,
RevisionImport,
RunCreate,
RunEvent,
RunListQuery,
RunSummary,
ShareTemplateRequest,
TemplateVersion,
TemplateVersionDetail,
UpgradeTemplateRequest,
User,
UUID,
Workflow,
WorkflowCreate,
WorkflowNodeType,
WorkflowRun,
WorkflowTemplate,
WorkflowUpdate,
WorkflowValidation,
} from './types';

type Opt = { signal?: AbortSignal };
const enc = encodeURIComponent;

// ─── system ────────────────────────────────────────────────────────────────

/** GET /healthz (outside /api/v1, no auth). */
export const getHealthz = (o: Opt = {}) => request<Healthz>('/healthz', { root: true, anonymous: true, signal: o.signal });

/** GET /system/capabilities — what is actually configured right now. */
export const getCapabilities = (o: Opt = {}) => request<Capabilities>('/system/capabilities', { signal: o.signal });

// ─── auth ──────────────────────────────────────────────────────────────────

/** POST /auth/login. A 401 here is "invalid_credentials", not a session expiry. */
export const login = (body: LoginRequest, o: Opt = {}) =>
  request<LoginResponse>('/auth/login', { method: 'POST', body, anonymous: true, quiet401: true, signal: o.signal });

/** POST /auth/logout (204). Revokes the current token server-side. */
export const logout = (o: Opt = {}) => request<void>('/auth/logout', { method: 'POST', quiet401: true, signal: o.signal });

/** GET /auth/me */
export const getMe = (o: Opt = {}) => request<User>('/auth/me', { signal: o.signal });

// ─── projects ──────────────────────────────────────────────────────────────

export const listProjects = (q: ProjectListQuery = {}, o: Opt = {}) =>
  request<Project[]>('/projects', { query: { include_archived: q.include_archived, q: q.q }, signal: o.signal });

export const createProject = (body: ProjectCreate, o: Opt = {}) => request<Project>('/projects', { method: 'POST', body, signal: o.signal });

export const getProject = (projectId: UUID, o: Opt = {}) => request<Project>(`/projects/${enc(projectId)}`, { signal: o.signal });

export const updateProject = (projectId: UUID, body: ProjectPatch, o: Opt = {}) =>
  request<Project>(`/projects/${enc(projectId)}`, { method: 'PATCH', body, signal: o.signal });

/** POST /projects/{id}/archive (?restore=true to restore). Owner only. */
export const archiveProject = (projectId: UUID, restore = false, o: Opt = {}) =>
  request<Project>(`/projects/${enc(projectId)}/archive`, { method: 'POST', query: { restore: restore || undefined }, signal: o.signal });

export const listMembers = (projectId: UUID, o: Opt = {}) => request<Member[]>(`/projects/${enc(projectId)}/members`, { signal: o.signal });

/** PUT /projects/{id}/members — add by email or change role. Owner only. */
export const upsertMember = (projectId: UUID, body: MemberUpsert, o: Opt = {}) =>
  request<Member>(`/projects/${enc(projectId)}/members`, { method: 'PUT', body, signal: o.signal });

/** DELETE /projects/{id}/members/{user_id}. 400 last_owner when removing the only owner. */
export const removeMember = (projectId: UUID, userId: UUID, o: Opt = {}) =>
  request<void>(`/projects/${enc(projectId)}/members/${enc(userId)}`, { method: 'DELETE', signal: o.signal });

// ─── parts ─────────────────────────────────────────────────────────────────

export const listParts = (projectId: UUID, q: PartListQuery = {}, o: Opt = {}) =>
  request<Part[]>(`/projects/${enc(projectId)}/parts`, {
    query: { q: q.q, status: q.status, tag: q.tag, folder: q.folder, include_archived: q.include_archived },
    signal: o.signal,
  });

export const createPart = (projectId: UUID, body: PartCreate, o: Opt = {}) =>
  request<Part>(`/projects/${enc(projectId)}/parts`, { method: 'POST', body, signal: o.signal });

export const getPart = (partId: UUID, o: Opt = {}) => request<Part>(`/parts/${enc(partId)}`, { signal: o.signal });

export const updatePart = (partId: UUID, body: PartPatch, o: Opt = {}) =>
  request<Part>(`/parts/${enc(partId)}`, { method: 'PATCH', body, signal: o.signal });

export const archivePart = (partId: UUID, restore = false, o: Opt = {}) =>
  request<Part>(`/parts/${enc(partId)}/archive`, { method: 'POST', query: { restore: restore || undefined }, signal: o.signal });

/** POST /parts/{id}/copy — files, revisions and drawings are copied; plans, quotes and approvals are not. */
export const copyPart = (partId: UUID, body: PartCopyRequest, o: Opt = {}) =>
  request<Part>(`/parts/${enc(partId)}/copy`, { method: 'POST', body, signal: o.signal });

// ─── files & blobs ─────────────────────────────────────────────────────────

/** POST /projects/{id}/files (multipart: file, kind?, part_id?, note?). Kind is guessed server-side when omitted. */
export const uploadFile = (projectId: UUID, up: FileUpload, o: Opt = {}) =>
  request<ProjectFile>(`/projects/${enc(projectId)}/files`, {
    method: 'POST',
    form: toFormData({ file: up.file, kind: up.kind, part_id: up.part_id ?? undefined, note: up.note }),
    signal: o.signal,
  });

export const listFiles = (projectId: UUID, q: FileListQuery = {}, o: Opt = {}) =>
  request<ProjectFile[]>(`/projects/${enc(projectId)}/files`, {
    query: { part_id: q.part_id, kind: q.kind, include_archived: q.include_archived },
    signal: o.signal,
  });

export const getFile = (fileId: UUID, o: Opt = {}) => request<ProjectFile>(`/files/${enc(fileId)}`, { signal: o.signal });

/** POST /files/{id}/versions (multipart: file, note?). */
export const addFileVersion = (fileId: UUID, up: FileVersionUpload, o: Opt = {}) =>
  request<ProjectFile>(`/files/${enc(fileId)}/versions`, {
    method: 'POST',
    form: toFormData({ file: up.file, note: up.note }),
    signal: o.signal,
  });

/** PATCH /files/{id}?name=&part_id=&archived= — rename, relink or archive (query parameters, no body). */
export const patchFile = (fileId: UUID, patch: FilePatch, o: Opt = {}) =>
  request<ProjectFile>(`/files/${enc(fileId)}`, {
    method: 'PATCH',
    query: { name: patch.name, part_id: patch.part_id, archived: patch.archived },
    signal: o.signal,
  });

/** GET /blobs/{sha256} with auth. Use api/blobs.ts for object URLs and downloads. */
export const getBlob = (sha256: string, o: Opt & { filename?: string } = {}) =>
  request<Blob>(`/blobs/${enc(sha256)}`, { query: { filename: o.filename }, as: 'blob', signal: o.signal });

// ─── model revisions (server/app/api/revisions.py) ─────────────────────────

/** GET /parts/{id}/revisions — newest first. */
export const listRevisions = (partId: UUID, o: Opt = {}) => request<ModelRevision[]>(`/parts/${enc(partId)}/revisions`, { signal: o.signal });

export const getRevision = (revisionId: UUID, o: Opt = {}) => request<ModelRevision>(`/revisions/${enc(revisionId)}`, { signal: o.signal });

/** POST /cad/preview — build without saving (editor live preview). 422 invalid_ops on bad ops. */
export const previewCad = (body: CadPreviewRequest, o: Opt = {}) =>
  request<CadPreviewResponse>('/cad/preview', { method: 'POST', body, signal: o.signal });

/** POST /parts/{id}/revisions — manual save. 422 build_failed / op_id_reused / invalid_ops. */
export const createRevision = (partId: UUID, body: RevisionCreate, o: Opt = {}) =>
  request<ModelRevision>(`/parts/${enc(partId)}/revisions`, { method: 'POST', body, signal: o.signal });

/** POST /parts/{id}/revisions/import — STEP becomes an editable base, STL/OBJ/GLB a mesh reference. */
export const importRevision = (partId: UUID, body: RevisionImport, o: Opt = {}) =>
  request<ModelRevision>(`/parts/${enc(partId)}/revisions/import`, { method: 'POST', body, signal: o.signal });

/** GET /revisions/{id}/diff?against= — defaults to the parent revision. */
export const diffRevision = (revisionId: UUID, against?: UUID, o: Opt = {}) =>
  request<RevisionDiff>(`/revisions/${enc(revisionId)}/diff`, { query: { against }, signal: o.signal });

// ─── drawings (server/app/api/drawings.py) ─────────────────────────────────

/** POST /revisions/{id}/drawings — cloud-generated SVG + DXF from that exact revision. */
export const createDrawing = (revisionId: UUID, config: DrawingConfig, o: Opt = {}) =>
  request<Drawing>(`/revisions/${enc(revisionId)}/drawings`, { method: 'POST', body: { config }, signal: o.signal });

/** GET /parts/{id}/drawings — newest first. */
export const listDrawings = (partId: UUID, o: Opt = {}) => request<Drawing[]>(`/parts/${enc(partId)}/drawings`, { signal: o.signal });


export const getDrawing = (drawingId: UUID, o: Opt = {}) => request<Drawing>(`/drawings/${enc(drawingId)}`, { signal: o.signal });

/**
 * GET /drawings/{id}/package.zip — the whole package (every page file, check reports, manifest.json) as the raw
 * authenticated Response (Content-Disposition names the file, X-Manifest-SHA256 the manifest). Use
 * api/blobs.ts packageZipUrl / downloadPackageZip.
 */
export const getDrawingPackage = (drawingId: UUID, o: Opt = {}) =>
  request<Response>(`/drawings/${enc(drawingId)}/package.zip`, { as: 'response', signal: o.signal });

// ─── manufacturing (server/app/api/manufacturing.py) ───────────────────────

/** GET /parts/{id}/status — current design, downstream staleness, confirmations, blockers. */
export const getPartStatus = (partId: UUID, o: Opt = {}) => request<PartPipelineStatus>(`/parts/${enc(partId)}/status`, { signal: o.signal });

/** GET /parts/{id}/intakes — drawing-only path, newest first. */
export const listIntakes = (partId: UUID, o: Opt = {}) => request<Intake[]>(`/parts/${enc(partId)}/intakes`, { signal: o.signal });

/** POST /parts/{id}/intakes — manual drawing intake (no AI, always estimated). */
export const createManualIntake = (partId: UUID, body: ManualIntakeCreate, o: Opt = {}) =>
  request<Intake>(`/parts/${enc(partId)}/intakes`, { method: 'POST', body, signal: o.signal });

/** POST /parts/{id}/process-plans — exactly one of model_revision_id / intake_id. */
export const createProcessPlan = (partId: UUID, body: ProcessPlanCreate, o: Opt = {}) =>
  request<ProcessPlan>(`/parts/${enc(partId)}/process-plans`, { method: 'POST', body, signal: o.signal });

export const listProcessPlans = (partId: UUID, o: Opt = {}) =>
  request<ProcessPlan[]>(`/parts/${enc(partId)}/process-plans`, { signal: o.signal });

export const getProcessPlan = (planId: UUID, o: Opt = {}) => request<ProcessPlan>(`/process-plans/${enc(planId)}`, { signal: o.signal });

/** POST /process-plans/{id}/quotes — 422 not_priceable when the plan has blocking gaps. */
export const createQuote = (planId: UUID, body: QuoteCreate, o: Opt = {}) =>
  request<Quote>(`/process-plans/${enc(planId)}/quotes`, { method: 'POST', body, signal: o.signal });

export const listQuotes = (partId: UUID, o: Opt = {}) => request<Quote[]>(`/parts/${enc(partId)}/quotes`, { signal: o.signal });

export const getQuote = (quoteId: UUID, o: Opt = {}) => request<Quote>(`/quotes/${enc(quoteId)}`, { signal: o.signal });

/** POST /approvals — 409 subject_changed / subject_stale when the subject moved on. */
export const createApproval = (body: ApprovalCreate, o: Opt = {}) =>
  request<ApprovalCreated>('/approvals', { method: 'POST', body, signal: o.signal });

/** GET /parts/{id}/approvals — newest first, `valid` = still current. */
export const listApprovals = (partId: UUID, o: Opt = {}) => request<Approval[]>(`/parts/${enc(partId)}/approvals`, { signal: o.signal });

// ─── workflows & runs (server/app/api/workflows.py) ────────────────────────

export const listWorkflowNodeTypes = (o: Opt = {}) => request<WorkflowNodeType[]>('/workflow-node-types', { signal: o.signal });

export const listWorkflowTemplates = (o: Opt = {}) => request<WorkflowTemplate[]>('/workflow-templates', { signal: o.signal });

/** Project templates; with `part_id`, that part's own workflows instead. */
export const listWorkflows = (projectId: UUID, q: { include_archived?: boolean; part_id?: UUID; kind?: 'tool' | 'collaboration' | 'all' } = {}, o: Opt = {}) =>
  request<Workflow[]>(`/projects/${enc(projectId)}/workflows`, { query: { include_archived: q.include_archived, part_id: q.part_id }, signal: o.signal });

/** POST /projects/{id}/workflows — from a graph or a template_key. */
export const createWorkflow = (projectId: UUID, body: WorkflowCreate, o: Opt = {}) =>
  request<Workflow>(`/projects/${enc(projectId)}/workflows`, { method: 'POST', body, signal: o.signal });

export const getWorkflow = (workflowId: UUID, o: Opt = {}) => request<Workflow>(`/workflows/${enc(workflowId)}`, { signal: o.signal });

/** PUT /workflows/{id} — drafts may be invalid (errors come back in `validation`). 409 version_conflict. */
export const updateWorkflow = (workflowId: UUID, body: WorkflowUpdate, o: Opt = {}) =>
  request<Workflow>(`/workflows/${enc(workflowId)}`, { method: 'PUT', body, signal: o.signal });

export const archiveWorkflow = (workflowId: UUID, restore = false, o: Opt = {}) =>
  request<Workflow>(`/workflows/${enc(workflowId)}/archive`, { method: 'POST', query: { restore: restore || undefined }, signal: o.signal });

export const validateWorkflow = (workflowId: UUID, o: Opt = {}) =>
  request<WorkflowValidation>(`/workflows/${enc(workflowId)}/validate`, { method: 'POST', signal: o.signal });

/**
 * POST /workflows/{id}/runs — runs a published version (template_version_id, default latest) or the
 * draft (draft: true). 409 template_not_published / template_version_deprecated, 422 input_quality /
 * invalid_graph / draft_run_needs_exploration / gate_param_locked, 400 part_required, 403 forbidden.
 */
export const startWorkflowRun = (workflowId: UUID, body: RunCreate, o: Opt = {}) =>
  request<WorkflowRun>(`/workflows/${enc(workflowId)}/runs`, { method: 'POST', body: runChoices(body), signal: o.signal });

/**
 * GET /projects/{id}/runs — newest first. Filters (execution monitor): task_id,
 * unlinked (no task), workflow_id, template_version_id, created_by, origin,
 * status (comma-separated), since; paged with limit / offset.
 */
export const listRuns = (projectId: UUID, q: RunListQuery = {}, o: Opt = {}) =>
  request<RunSummary[]>(`/projects/${enc(projectId)}/runs`, {
    query: {
      part_id: q.part_id,
      status: q.status,
      task_id: q.task_id,
      unlinked: q.unlinked || undefined,
      workflow_id: q.workflow_id,
      template_version_id: q.template_version_id,
      created_by: q.created_by,
      origin: q.origin,
      since: q.since,
      limit: q.limit,
      offset: q.offset || undefined,
    },
    signal: o.signal,
  });

/**
 * POST /runs/{id}/rerun — a new run with rerun_of = this one (same task and part). Defaults: the original
 * version, input-node params, title and purpose. 422 template_version_required (legacy / draft source),
 * 409 template_version_deprecated, 409 rerun_unsupported (workspace runs).
 */
export const rerunRun = (runId: UUID, body: RerunRequest, o: Opt = {}) =>
  request<WorkflowRun>(`/runs/${enc(runId)}/rerun`, { method: 'POST', body: runChoices(body), signal: o.signal });

/** Create a clean draft from a succeeded run; identical request retries return the same template. */
export const createTemplateFromRun = (runId: UUID, body: { name: string; client_request_id: UUID }, o: Opt = {}) =>
  request<Workflow>(`/runs/${enc(runId)}/template`, {
    method: 'POST',
    body: { name: body.name, client_request_id: body.client_request_id },
    signal: o.signal,
  });

// ─── template versions (P3: publish, deprecate, history) ───────────────────

/** POST /workflows/{id}/publish — 409 version_conflict / no_changes, 422 template_invalid (details.errors). */
export const publishWorkflow = (workflowId: UUID, body: PublishRequest, o: Opt = {}) =>
  request<TemplateVersionDetail>(`/workflows/${enc(workflowId)}/publish`, { method: 'POST', body, signal: o.signal });

/** GET /workflows/{id}/versions — newest first. */
export const listTemplateVersions = (workflowId: UUID, o: Opt = {}) =>
  request<TemplateVersion[]>(`/workflows/${enc(workflowId)}/versions`, { signal: o.signal });

export const getTemplateVersion = (versionId: UUID, o: Opt = {}) =>
  request<TemplateVersionDetail>(`/template-versions/${enc(versionId)}`, { signal: o.signal });

/** POST /template-versions/{vid}/deprecate — the version can no longer start runs; history stays. */
export const deprecateTemplateVersion = (versionId: UUID, reason: string, o: Opt = {}) =>
  request<TemplateVersion>(`/template-versions/${enc(versionId)}/deprecate`, { method: 'POST', body: { reason }, signal: o.signal });

/** GET /template-versions/{vid}/runs — usage history of one version. */
export const listTemplateVersionRuns = (versionId: UUID, o: Opt = {}) =>
  request<RunSummary[]>(`/template-versions/${enc(versionId)}/runs`, { signal: o.signal });

// ─── public template library (api/template_library.py) ─────────────────────

export const listTemplateLibrary = (q: { q?: string } = {}, o: Opt = {}) =>
  request<PublicTemplate[]>('/template-library', { query: { q: q.q }, signal: o.signal });

export const getPublicTemplate = (publicTemplateId: UUID, o: Opt = {}) =>
  request<PublicTemplateDetail>(`/template-library/${enc(publicTemplateId)}`, { signal: o.signal });

/** POST /template-library — share one published project version as a new public template (v1). */
export const shareTemplateToLibrary = (body: ShareTemplateRequest, o: Opt = {}) =>
  request<PublicTemplateDetail>('/template-library', { method: 'POST', body, signal: o.signal });

/** POST /template-library/{id}/versions — append a public version (creator or admin). 409 no_changes. */
export const addPublicTemplateVersion = (publicTemplateId: UUID, body: { template_version_id: UUID; notes: string }, o: Opt = {}) =>
  request<PublicTemplateDetail>(`/template-library/${enc(publicTemplateId)}/versions`, { method: 'POST', body, signal: o.signal });

export const deprecatePublicTemplateVersion = (publicVersionId: UUID, o: Opt = {}) =>
  request<PublicTemplateVersion>(`/template-library/versions/${enc(publicVersionId)}/deprecate`, { method: 'POST', signal: o.signal });

/** POST /projects/{id}/workflows/import — new project template + its v1 from a public version. */
export const importPublicTemplate = (projectId: UUID, body: ImportTemplateRequest, o: Opt = {}) =>
  request<Workflow>(`/projects/${enc(projectId)}/workflows/import`, { method: 'POST', body, signal: o.signal });

/** POST /workflows/{id}/upgrade — adopt a newer public version (replaces the draft, publishes a new project version). */
export const upgradeWorkflow = (workflowId: UUID, body: UpgradeTemplateRequest, o: Opt = {}) =>
  request<Workflow>(`/workflows/${enc(workflowId)}/upgrade`, { method: 'POST', body, signal: o.signal });

export const getRun = (runId: UUID, o: Opt = {}) => request<WorkflowRun>(`/runs/${enc(runId)}`, { signal: o.signal });

/** GET /runs/{id}/ai-calls — model-call ledger of the run (metadata only, oldest first; project members). */
export const listRunAiCalls = (runId: UUID, o: Opt = {}) => request<AiCallRecord[]>(`/runs/${enc(runId)}/ai-calls`, { signal: o.signal });

/** POST /runs/{id}/cancel — 409 run_finished. */
export const cancelRun = (runId: UUID, o: Opt = {}) => request<WorkflowRun>(`/runs/${enc(runId)}/cancel`, { method: 'POST', signal: o.signal });

/** POST /runs/{id}/nodes/{node_id}/decision — human review step. */
export const decideRunNode = (runId: UUID, nodeId: string, body: NodeDecision, o: Opt = {}) =>
  request<WorkflowRun>(`/runs/${enc(runId)}/nodes/${enc(nodeId)}/decision`, { method: 'POST', body, signal: o.signal });

/** POST /runs/{id}/nodes/{node_id}/retry — 409 cannot_retry. */
export const retryRunNode = (runId: UUID, nodeId: string, o: Opt = {}) =>
  request<WorkflowRun>(`/runs/${enc(runId)}/nodes/${enc(nodeId)}/retry`, { method: 'POST', signal: o.signal });

/**
 * GET /runs/{id}/events (Server-Sent Events). The server sends `event: run`
 * with a full WorkflowRun snapshot whenever something changes, and closes the
 * stream when the run reaches a terminal state. With `progress`, also
 * `event: progress` with the run's progress view (api/progress.ts).
 *
 * EventSource cannot send an Authorization header, so this reads the stream
 * with fetch and parses SSE frames. Resolves when the stream ends; abort the
 * signal to stop. Errors are ApiError like every other call.
 */
export async function subscribeRunEvents(runId: UUID, onEvent: (e: RunEvent) => void, o: Opt & { progress?: boolean } = {}): Promise<void> {
  const headers: Record<string, string> = { Accept: 'text/event-stream' };
  const token = currentToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let res: Response;
  try {
    res = await fetch(buildUrl(`/runs/${enc(runId)}/events${o.progress ? '?progress=true' : ''}`), { headers, signal: o.signal });
  } catch (e) {
    if (isAbortError(e)) return;
    throw new ApiError(0, 'network', e instanceof Error ? e.message : 'network request failed');
  }
  if (!res.ok) throw parseErrorBody(res.status, await res.text().catch(() => ''), res.statusText);
  if (!res.body) return;
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = createSseParser(onEvent);
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      parser(value);
    }
  } catch (e) {
    if (!isAbortError(e)) throw e;
  }
}

/** Typed convenience over subscribeRunEvents: calls back with each run snapshot. */
export function watchRun(runId: UUID, onRun: (run: WorkflowRun) => void, o: Opt = {}): Promise<void> {
  return subscribeRunEvents(runId, (e) => {
    if (e.event === 'run' && e.data && typeof e.data === 'object') onRun(e.data as WorkflowRun);
  }, o);
}

/** Incremental SSE frame parser (exported for reuse). */
export function createSseParser(onEvent: (e: RunEvent) => void): (chunk: string) => void {
  let buf = '';
  let event = 'message';
  let id: string | undefined;
  let data: string[] = [];
  const dispatch = () => {
    if (data.length) {
      const raw = data.join('\n');
      let parsed: unknown = raw;
      try {
        parsed = JSON.parse(raw);
      } catch {
        parsed = raw;
      }
      onEvent({ event, id, data: parsed });
    }
    event = 'message';
    data = [];
  };
  return (chunk: string) => {
    buf += chunk;
    let nl: number;
    while ((nl = buf.search(/\r?\n/)) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + (buf[nl] === '\r' ? 2 : 1));
      if (line === '') dispatch();
      else if (line.startsWith(':')) continue;
      else {
        const i = line.indexOf(':');
        const field = i < 0 ? line : line.slice(0, i);
        const value = i < 0 ? '' : line.slice(i + 1).replace(/^ /, '');
        if (field === 'event') event = value;
        else if (field === 'data') data.push(value);
        else if (field === 'id') id = value;
      }
    }
  };
}

// ─── model configs (server/app/api/ai.py) ──────────────────────────────────

/** GET /model-configs — every signed-in user may list (never keys); writes are admin-only. */
export const listModelConfigs = (o: Opt = {}) => request<ModelConfig[]>('/model-configs', { signal: o.signal });

export const createModelConfig = (body: ModelConfigCreate, o: Opt = {}) =>
  request<ModelConfig>('/model-configs', { method: 'POST', body, signal: o.signal });

export const updateModelConfig = (id: UUID, body: ModelConfigPatch, o: Opt = {}) =>
  request<ModelConfig>(`/model-configs/${enc(id)}`, { method: 'PATCH', body, signal: o.signal });

/** POST /model-configs/models — the models an endpoint offers to a key (admins; a listing call, nothing saved). */
export const listProviderModels = (body: ModelListRequest, o: Opt = {}) =>
  request<ModelListResult>('/model-configs/models', { method: 'POST', body, signal: o.signal });

/** POST /model-configs/{id}/test — live connection test; returns the config with `last_test` updated. */
export const testModelConfig = (id: UUID, o: Opt = {}) =>
  request<ModelConfig>(`/model-configs/${enc(id)}/test`, { method: 'POST', signal: o.signal });
