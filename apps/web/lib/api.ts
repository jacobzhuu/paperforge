import type {
  ApiResult,
  CreateProjectRequest,
  Job,
  JobEvent,
  LibraryEntry,
  LibraryEntryStatus,
  LibraryPdfUpload,
  LibraryPdfUploadResult,
  LiteratureRole,
  Project,
  ProjectTaskProfile,
  TaskDefinitionSummary,
  CitationAudit,
  CostDetail,
  EligibilityDecision,
  EvidenceMatrix,
  EvidenceStance,
  EvidenceUnit,
  ExportArtifact,
  RequestableExportFormat,
  MarkdownPreview,
  NumLintReport,
  OutlinePayload,
  OutlineTree,
  PaperSection,
  ProjectCost,
  ResearchQuestion,
  QualityReport,
  QualityProfile,
  ReviewStyle,
  ClaimEvidence,
  RefineAction,
  RefineResult,
  SynthesisPayload,
  ScopePayload,
  SearchRun,
  SubmissionReadiness,
  RuntimeSettings,
  SectionIR,
  UpdateProjectRequest,
  UserAsset,
  DocumentVersion,
  VersionHistory,
  VisualAsset,
  VisualDraft,
  VisualSummary,
  CreateVisualRequest,
  RegenerateVisualRequest,
  AuthUser,
  AcademicProfile,
  AuthorDetail,
  AssetCapabilities,
  MaterialPreflight,
  RewriteSectionCandidate,
} from './types';
import { MOCK_LIBRARY, MOCK_PROJECTS, MOCK_SEARCH_RUNS } from './mock';

// 生产浏览器只能访问同源 /api/v1，由 Next/reverse proxy 转发到 FastAPI。
export const API_BASE =
  process.env.NODE_ENV === 'production'
    ? ''
    : process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, '') ?? '';

export const DEMO_MODE =
  process.env.NODE_ENV !== 'production' && process.env.NEXT_PUBLIC_DEMO_MODE === 'true';

const API_PREFIX = '/api/v1';

class DegradeError extends Error {
  constructor(public reason: string) {
    super(reason);
  }
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public detail?: unknown,
  ) {
    super(message);
  }
}

function notifyAuthenticationFailure(): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new Event('paperforge:auth-expired'));
}

/**
 * 统一请求；只有显式开发演示模式会将网络错误、501 或 404 转为示例数据。
 * - 其他非 2xx 抛出普通 Error，交由调用方决定是否提示。
 */
const inflightGets = new Map<string, Promise<unknown>>();

/**
 * 同一渲染周期里多个模块经常读取同一资源（例如 sections 同时供导航进度和写作台）。
 * 只合并仍在途的 GET，不缓存响应，也不合并带 AbortSignal 的独立生命周期请求。
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? 'GET').toUpperCase();
  if (method !== 'GET' || init?.signal) return executeRequest<T>(path, init);
  const existing = inflightGets.get(path);
  if (existing) return existing as Promise<T>;
  const pending = executeRequest<T>(path, init).finally(() => {
    if (inflightGets.get(path) === pending) inflightGets.delete(path);
  });
  inflightGets.set(path, pending);
  return pending;
}

async function executeRequest<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    const headers = new Headers(init?.headers);
    const isFormData =
      typeof FormData !== 'undefined' && init?.body instanceof FormData;
    if (!isFormData && !headers.has('Content-Type')) {
      headers.set('Content-Type', 'application/json');
    }
    res = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers,
      cache: 'no-store',
      credentials: 'include',
    });
  } catch {
    if (DEMO_MODE) throw new DegradeError('无法连接后端 API');
    throw new Error('无法连接后端 API');
  }
  if (res.status === 401) {
    notifyAuthenticationFailure();
  }
  if (DEMO_MODE && (res.status === 501 || res.status === 404)) {
    throw new DegradeError(`后端端点未实现（${res.status}）`);
  }
  if (!res.ok) {
    const payload = await res.json().catch(() => undefined) as
      | { detail?: string | { code?: string; message?: string } }
      | undefined;
    const detail = payload?.detail;
    const code = typeof detail === 'object' ? detail.code ?? 'request_failed' : 'request_failed';
    const message =
      typeof detail === 'object'
        ? detail.message ?? code
        : detail ?? res.statusText ?? `API ${res.status}`;
    throw new ApiError(res.status, code, message, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/**
 * 只有在**后端确实不可用**时才回退到示例数据。
 *
 * 空结果是合法的真实数据，绝不用示例数据填充：本系统的立身之本是「引用必须真实」，
 * 往界面里塞看起来一模一样的假文献，比空列表危险得多。空态由各页面自己呈现。
 */
async function withFallback<T>(
  live: () => Promise<T>,
  fallback: T,
): Promise<ApiResult<T>> {
  try {
    const data = await live();
    return { data, source: 'live' };
  } catch (err) {
    if (err instanceof DegradeError) {
      return { data: fallback, source: 'mock', note: err.reason };
    }
    throw err;
  }
}

// ---- Authentication ----

export function getCurrentUser(): Promise<AuthUser> {
  return request<AuthUser>('/auth/me');
}

export function getAcademicProfile(): Promise<AcademicProfile> {
  return request<AcademicProfile>('/auth/me/academic-profile');
}

export function updateAcademicProfile(profile: AuthorDetail | null): Promise<AcademicProfile> {
  return request<AcademicProfile>('/auth/me/academic-profile', {
    method: 'PATCH',
    body: JSON.stringify({ profile }),
  });
}

export async function registerAccount(body: {
  email: string;
  password: string;
  display_name?: string;
}): Promise<string> {
  const result = await request<{ message: string }>('/auth/register', {
    method: 'POST',
    body: JSON.stringify(body),
  });
  return result.message;
}

export async function verifyEmail(token: string): Promise<string> {
  const result = await request<{ message: string }>('/auth/verify-email', {
    method: 'POST',
    body: JSON.stringify({ token }),
  });
  return result.message;
}

export async function resendVerification(email: string): Promise<string> {
  const result = await request<{ message: string }>('/auth/resend-verification', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });
  return result.message;
}

export async function loginAccount(account: string, password: string): Promise<AuthUser> {
  const result = await request<{ user: AuthUser }>('/auth/login', {
    method: 'POST',
    // The API keeps the historical `email` key but accepts a local development username.
    body: JSON.stringify({ email: account, password }),
  });
  return result.user;
}

export function logoutAccount(): Promise<void> {
  return request<void>('/auth/logout', { method: 'POST' });
}

export async function requestPasswordReset(email: string): Promise<string> {
  const result = await request<{ message: string }>('/auth/forgot-password', {
    method: 'POST',
    body: JSON.stringify({ email }),
  });
  return result.message;
}

export async function resetPassword(token: string, newPassword: string): Promise<string> {
  const result = await request<{ message: string }>('/auth/reset-password', {
    method: 'POST',
    body: JSON.stringify({ token, new_password: newPassword }),
  });
  return result.message;
}

// ---- Projects ----

export function listProjects(): Promise<ApiResult<Project[]>> {
  return withFallback(() => request<Project[]>('/projects'), MOCK_PROJECTS);
}

export function getAssetCapabilities(signal?: AbortSignal): Promise<AssetCapabilities> {
  return request<AssetCapabilities>('/assets/capabilities', { signal });
}

export function getMaterialPreflight(
  projectId: string,
  signal?: AbortSignal,
): Promise<MaterialPreflight> {
  return request<MaterialPreflight>(`/projects/${projectId}/assets/preflight`, { signal });
}

export function rewriteSectionCandidate(
  projectId: string,
  sectionKey: string,
  instruction: string,
  expectedUpdatedAt?: string | null,
): Promise<RewriteSectionCandidate> {
  return request<RewriteSectionCandidate>(`/projects/${projectId}/sections/${sectionKey}/rewrite-candidate`, {
    method: 'POST',
    body: JSON.stringify({ instruction, expected_updated_at: expectedUpdatedAt ?? undefined }),
  });
}

export function acceptSectionRewrite(
  projectId: string,
  sectionKey: string,
  candidateBodyIr: SectionIR,
  expectedUpdatedAt?: string | null,
): Promise<{ document_version: number; section: PaperSection }> {
  return request(`/projects/${projectId}/sections/${sectionKey}/rewrite-accept`, {
    method: 'POST',
    body: JSON.stringify({ candidate_body_ir: candidateBodyIr, expected_updated_at: expectedUpdatedAt ?? undefined }),
  });
}

export async function getProject(
  id: string,
  signal?: AbortSignal,
): Promise<ApiResult<Project | undefined>> {
  return withFallback(
    () => request<Project>(`/projects/${id}`, { signal }),
    MOCK_PROJECTS.find((p) => p.id === id),
  );
}

export function createProject(body: CreateProjectRequest): Promise<ApiResult<Project>> {
  const optimistic: Project = {
    id: `local-${Date.now()}`,
    title: body.title,
    paper_type: body.paper_type,
    writing_mode: body.writing_mode,
    language: body.language,
    status: 'draft',
    venue_template: body.venue_template ?? null,
    citation_style: body.citation_style,
    topic: body.topic ?? null,
    contribution_points: body.contribution_points ?? [],
    library_count: 0,
    section_count: 0,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  };
  return withFallback(
    () =>
      request<Project>('/projects', {
        method: 'POST',
        body: JSON.stringify(body),
      }),
    optimistic,
  );
}

/**
 * 改项目元数据（题目 / 主题 / 模板 / 语言 / 引用样式 / 写作模式）。
 *
 * **刻意不给降级桩**：`createProject` 那种「后端不可用就返回本地乐观对象」的做法
 * 在这里是有害的——用户会看到题目改好了，刷新后发现根本没存。改名要么真落库，
 * 要么明确报错，所以这里让 DegradeError 直接抛给调用方。
 */
export async function updateProject(
  id: string,
  body: UpdateProjectRequest,
): Promise<Project> {
  return request<Project>(`/projects/${id}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

/** 回收站：只列已删除的项目。 */
export function listDeletedProjects(): Promise<ApiResult<Project[]>> {
  return withFallback(() => request<Project[]>('/projects?deleted=true'), []);
}

/**
 * 删除项目（软删除，可恢复）。项目里在跑的任务会被后端一并取消。
 *
 * 不走 withFallback：删除是破坏性的明确指令，后端不可用时必须报错。
 * 让用户以为删掉了、刷新后项目还在，比直接报错糟糕得多。
 */
export function deleteProject(id: string): Promise<void> {
  return request<void>(`/projects/${id}`, { method: 'DELETE' });
}

/** 从回收站恢复项目。 */
export function restoreProject(id: string): Promise<Project> {
  return request<Project>(`/projects/${id}/restore`, { method: 'POST' });
}

// ---- Library ----

export interface WebResearchSource {
  id: string;
  url: string;
  title: string;
  snippet: string;
  body: string;
  status: string;
  fetched_at: string | null;
  verification: { identifiers: { verified: number; doi?: string; arxiv_id?: string }[] } | null;
}

export interface WebResearchRun {
  id: string;
  status: string;
  queries: string[];
  calls: number;
  error: string | null;
  created_at: string;
  finished_at: string | null;
  sources?: WebResearchSource[];
}

export function listWebResearch(projectId: string, offset = 0) {
  return request<{ available: boolean; runs: WebResearchRun[] }>(
    `/projects/${projectId}/web-research/runs?offset=${offset}`,
  );
}

export function getWebResearch(projectId: string, runId: string) {
  return request<WebResearchRun>(`/projects/${projectId}/web-research/runs/${runId}`);
}

export function refreshWebResearch(projectId: string) {
  return request<Job>(`/projects/${projectId}/web-research/runs`, { method: 'POST' });
}

export function listLibrary(
  projectId: string,
  status?: string,
  signal?: AbortSignal,
): Promise<ApiResult<LibraryEntry[]>> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : '';
  return withFallback(
    () => request<LibraryEntry[]>(`/projects/${projectId}/library${qs}`, { signal }),
    MOCK_LIBRARY,
  );
}

export function listSearchRuns(projectId: string): Promise<ApiResult<SearchRun[]>> {
  return withFallback(
    () => request<SearchRun[]>(`/projects/${projectId}/search/runs`),
    MOCK_SEARCH_RUNS,
  );
}

export function listPdfUploads(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<LibraryPdfUpload[]>> {
  return withFallback(
    () => request<LibraryPdfUpload[]>(`/projects/${projectId}/library/pdf-uploads`, { signal }),
    [],
  );
}

/** 学术 PDF 走文献链路，绝不写入 user_asset。 */
export function uploadLibraryPdf(
  projectId: string,
  file: File,
): Promise<LibraryPdfUploadResult> {
  const form = new FormData();
  form.append('file', file);
  return request<LibraryPdfUploadResult>(`/projects/${projectId}/library/pdf-uploads`, {
    method: 'POST',
    body: form,
  });
}

export function confirmPdfUpload(
  projectId: string,
  uploadId: string,
  literatureRole: LiteratureRole,
): Promise<LibraryPdfUploadResult> {
  return request<LibraryPdfUploadResult>(
    `/projects/${projectId}/library/pdf-uploads/${uploadId}/confirm`,
    {
      method: 'POST',
      body: JSON.stringify({ literature_role: literatureRole }),
    },
  );
}

export function retryPdfUpload(
  projectId: string,
  uploadId: string,
): Promise<LibraryPdfUploadResult> {
  return request<LibraryPdfUploadResult>(
    `/projects/${projectId}/library/pdf-uploads/${uploadId}/retry`,
    { method: 'POST' },
  );
}

export function rejectPdfUpload(projectId: string, uploadId: string): Promise<void> {
  return request<void>(`/projects/${projectId}/library/pdf-uploads/${uploadId}`, {
    method: 'DELETE',
  });
}

// ---- Scope ----

export function getScope(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<ScopePayload | undefined>> {
  return withFallback(
    () => request<{ project_id: string; scope: ScopePayload }>(`/projects/${projectId}/scope`, { signal })
      .then((r) => r.scope),
    undefined,
  );
}

export function generateScope(
  projectId: string,
  topic?: string,
): Promise<ApiResult<ScopePayload | undefined>> {
  return withFallback(
    () =>
      request<{ project_id: string; scope: ScopePayload }>(
        `/projects/${projectId}/scope/generate`,
        { method: 'POST', body: JSON.stringify({ topic: topic ?? null }) },
      ).then((r) => r.scope),
    undefined,
  );
}

export function listTaskDefinitions(
  signal?: AbortSignal,
): Promise<ApiResult<TaskDefinitionSummary[]>> {
  return withFallback(() => request<TaskDefinitionSummary[]>('/tasks', { signal }), []);
}

export function getProjectTasks(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<ProjectTaskProfile | undefined>> {
  return withFallback(
    () => request<ProjectTaskProfile>(`/projects/${projectId}/tasks`, { signal }),
    undefined,
  );
}

export function updateProjectTasks(
  projectId: string,
  taskIds: string[],
): Promise<ApiResult<ProjectTaskProfile | undefined>> {
  return withFallback(
    () =>
      request<ProjectTaskProfile>(`/projects/${projectId}/tasks`, {
        method: 'PUT',
        body: JSON.stringify({ task_ids: taskIds }),
      }),
    undefined,
  );
}

export function updateScope(
  projectId: string,
  scope: ScopePayload,
): Promise<ApiResult<ScopePayload | undefined>> {
  return withFallback(
    () =>
      request<{ project_id: string; scope: ScopePayload }>(`/projects/${projectId}/scope`, {
        method: 'PUT',
        body: JSON.stringify({ scope }),
      }).then((r) => r.scope),
    scope,
  );
}

// ---- Jobs（检索 / 导入 / 卡片都是异步任务） ----

export function startSearch(
  projectId: string,
  options: { providers?: string[]; regenerateScope?: boolean } = {},
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/search/runs`, {
        method: 'POST',
        body: JSON.stringify({
          providers: options.providers ?? null,
          regenerate_scope: options.regenerateScope ?? false,
        }),
      }),
    undefined,
  );
}

export function importReferences(
  projectId: string,
  payload: { dois?: string[]; bibtex?: string },
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/library/import`, {
        method: 'POST',
        body: JSON.stringify({ dois: payload.dois ?? [], bibtex: payload.bibtex ?? null }),
      }),
    undefined,
  );
}

export function generateCards(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/cards/generate`, { method: 'POST' }),
    undefined,
  );
}

export function getJob(projectId: string, jobId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(() => request<Job>(`/projects/${projectId}/jobs/${jobId}`), undefined);
}

export function listJobs(projectId: string, signal?: AbortSignal): Promise<ApiResult<Job[]>> {
  return withFallback(() => request<Job[]>(`/projects/${projectId}/jobs`, { signal }), []);
}

/**
 * 跳过剩余的连贯性润色。不走 withFallback：这是用户的一次明确指令，
 * 后端不可用时必须报错，而不是静悄悄地"看起来成功了"。
 */
export function skipPolish(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/polish/skip`, { method: 'POST' });
}

/** 首轮全流程完成后显式启动润色；返回独立的润色任务。 */
export function startPolish(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/polish`, { method: 'POST' });
}

/**
 * 全流程交付之后启动一轮质量修复；返回独立的修复任务。
 *
 * 和 startPolish 一样不走 withFallback：这是用户看过问题清单后的明确决定。
 */
export function startQualityRepair(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/quality-repair`, { method: 'POST' });
}

/** 记下「这一轮不修了」，避免概览页每次刷新都再邀请一遍。 */
export function skipQualityRepair(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/quality-repair/skip`, {
    method: 'POST',
  });
}

/**
 * 取消任务：已产出的内容全部保留，这一轮不再往下跑。
 *
 * 协作式停止——worker 跑完当前这一步（阶段/章节/条目）才会真的退出，
 * 所以点完之后最多还要等一两分钟，按钮文案必须说清这一点。
 */
export function cancelJob(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/cancel`, { method: 'POST' });
}

/** 暂停任务：停在最近的安全点，断点保留，可用 resumeJob 接着跑。 */
export function pauseJob(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/pause`, { method: 'POST' });
}

/** 从断点继续。返回的是**新建**的那个 job——已完成的阶段会被跳过。 */
export function resumeJob(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/resume`, { method: 'POST' });
}

/** 重连退避（毫秒）；任务运行期间另有 getJob 周期校准兜底。 */
const SSE_RETRY_DELAYS = [1000, 2000, 4000, 8000];
/** SSE 健康时轮询只做低频终态校准；断线后才提升频率。 */
export const HEALTHY_JOB_POLL_INTERVAL = 15_000;
export const DEGRADED_JOB_POLL_INTERVAL = 3_000;
const TERMINAL_JOB_STATUSES = new Set([
  'paused',
  'succeeded',
  'failed',
  'cancelled',
  'needs_input',
]);

export interface JobStreamHandlers {
  onEvent?: (event: JobEvent) => void;
  /** 任务真正结束（收到 job.closed 或轮询到终态）。 */
  onClose?: () => void;
  /** 连接中断中：reconnecting=true 表示正在重试，false 表示已恢复。 */
  onConnectionChange?: (state: { reconnecting: boolean; degradedToPolling: boolean }) => void;
}

/**
 * 订阅任务进度（SSE），断线自动重连，并用 getJob 周期校准。返回取消函数。
 *
 * 后端以无名事件下发（见 services/api/.../events.py::_format_event），因此这里只需
 * onmessage 一个入口——不再有「白名单漏了某个阶段 ⇒ 进度整段静默丢失」的可能。
 *
 * onerror 绝不能等同于 onClose：休眠、代理空闲超时、CORS 抖动都会触发 onerror，
 * 而任务在服务端仍在跑。把它当成功收尾会让十分钟的检索「看起来完成了」。
 *
 * 轮询不能只在 onerror 后才启动：Safari / 代理可能把 SSE 留在“连接已打开、消息却不再
 * 下发”的半断开状态，此时 onerror 永远不来。任务运行期间固定校准一次 job 状态，
 * SSE 负责细粒度事件，轮询负责保证阶段、百分比和终态最终一定能追上服务端。
 */
export function subscribeJobEvents(
  projectId: string,
  jobId: string,
  handlers: JobStreamHandlers,
): () => void {
  if (typeof window === 'undefined' || typeof EventSource === 'undefined') return () => {};

  const baseUrl = `${API_BASE}${API_PREFIX}/projects/${projectId}/jobs/${jobId}/events`;
  let source: EventSource | null = null;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let pollInterval = 0;
  let pollInFlight = false;
  let attempt = 0;
  let lastSeq = 0;
  let stopped = false;
  let degradedPolling = false;

  const finish = () => {
    if (stopped) return;
    stopped = true;
    cleanup();
    handlers.onClose?.();
  };

  const cleanup = () => {
    source?.close();
    source = null;
    if (retryTimer) clearTimeout(retryTimer);
    if (pollTimer) clearInterval(pollTimer);
    retryTimer = null;
    pollTimer = null;
    pollInterval = 0;
    document.removeEventListener('visibilitychange', onVisibilityChange);
  };

  /**
   * SSE 的安全网：即使流连接“假在线”，也会在一个轮询周期内校准到数据库状态。
   * 防止慢请求重叠；单次失败留给下一周期，不影响 SSE 本身。
   */
  const pollJob = async () => {
    if (stopped || pollInFlight) return;
    pollInFlight = true;
    try {
      const { data } = await getJob(projectId, jobId);
      if (stopped || !data) return;
      handlers.onEvent?.({
        seq: lastSeq,
        type: 'job.polled',
        payload: {},
        stage: data.stage,
        progress: data.progress,
        status: data.status,
      });
      if (data.status && TERMINAL_JOB_STATUSES.has(data.status)) finish();
    } catch {
      /* 校准失败留给下一周期；实时流可能仍然正常。 */
    } finally {
      pollInFlight = false;
    }
  };

  const startPolling = (degraded = false) => {
    if (stopped) return;
    degradedPolling = degraded;
    if (degraded) {
      handlers.onConnectionChange?.({ reconnecting: true, degradedToPolling: true });
    }
    if (document.hidden) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      pollInterval = 0;
      return;
    }
    const nextInterval = degraded
      ? DEGRADED_JOB_POLL_INTERVAL
      : HEALTHY_JOB_POLL_INTERVAL;
    if (pollTimer && pollInterval === nextInterval) return;
    if (pollTimer) clearInterval(pollTimer);
    pollInterval = nextInterval;
    pollTimer = setInterval(() => void pollJob(), nextInterval);
  };

  function onVisibilityChange() {
    if (stopped) return;
    if (document.hidden) {
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      pollInterval = 0;
      return;
    }
    // 休眠期间可能已经结束；先立即校准，再恢复对应频率。
    void pollJob();
    startPolling(degradedPolling);
  }

  const connect = () => {
    if (stopped) return;
    const url = lastSeq > 0 ? `${baseUrl}?after=${lastSeq}` : baseUrl;
    const es = new EventSource(url, { withCredentials: true });
    source = es;

    es.onopen = () => {
      degradedPolling = false;
      startPolling(false);
      handlers.onConnectionChange?.({ reconnecting: false, degradedToPolling: false });
    };

    es.onmessage = (raw: MessageEvent) => {
      let event: JobEvent;
      try {
        event = JSON.parse(raw.data) as JobEvent;
      } catch {
        return; // 单条事件解析失败不应打断进度流
      }
      if (typeof event.seq === 'number' && event.seq > lastSeq) lastSeq = event.seq;
      // 真正收到一条消息才说明这条连接可以传输数据；仅 onopen 不足以证明。
      attempt = 0;
      if (degradedPolling) startPolling(false);
      handlers.onEvent?.(event);
      if (event.type === 'job.closed') finish();
    };

    es.onerror = () => {
      if (stopped) return;
      es.close();
      source = null;
      // EventSource 自带重连，但不受控且不带 ?after=；这里自己退避重连以保证续传。
      if (attempt < SSE_RETRY_DELAYS.length) {
        handlers.onConnectionChange?.({ reconnecting: true, degradedToPolling: false });
        retryTimer = setTimeout(connect, SSE_RETRY_DELAYS[attempt]);
        attempt += 1;
      } else {
        startPolling(true);
      }
    };
  };

  document.addEventListener('visibilitychange', onVisibilityChange);
  connect();
  startPolling();

  return () => {
    stopped = true;
    cleanup();
  };
}

// ---- Library ----

export function selectEntries(
  projectId: string,
  workIds: string[],
  status: LibraryEntryStatus = 'selected',
): Promise<ApiResult<LibraryEntry[]>> {
  return withFallback(
    () =>
      request<LibraryEntry[]>(`/projects/${projectId}/library/entries`, {
        method: 'POST',
        body: JSON.stringify({ work_ids: workIds, status }),
      }),
    [],
  );
}

/** 单条的“纳入写作”和核心角色共用一个明确的 PATCH，不污染批量复选状态。 */
export function updateLibraryEntry(
  projectId: string,
  entryId: string,
  changes: { literature_role: LiteratureRole },
): Promise<LibraryEntry> {
  return request<LibraryEntry>(`/projects/${projectId}/library/entries/${entryId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });
}

/** 从文献库移除条目（后端 DELETE /library/entries/{id} 一直存在，此前无 UI 入口）。 */
export function deleteLibraryEntry(projectId: string, entryId: string): Promise<ApiResult<void>> {
  return withFallback(
    () =>
      request<void>(`/projects/${projectId}/library/entries/${entryId}`, { method: 'DELETE' }),
    undefined as unknown as void,
  );
}

/** R1 写作白名单：引用 chip 的唯一取值来源。 */
export function getWhitelist(projectId: string, signal?: AbortSignal): Promise<ApiResult<string[]>> {
  return withFallback(
    () =>
      request<{ project_id: string; cite_keys: string[] }>(
        `/projects/${projectId}/library/whitelist`,
        { signal },
      ).then((r) => r.cite_keys),
    [],
  );
}

export function getCost(projectId: string): Promise<ApiResult<ProjectCost | undefined>> {
  return withFallback(() => request<ProjectCost>(`/projects/${projectId}/cost`), undefined);
}

// ---- Outline / Sections / 审计 / 预览（M2） ----

export function getOutline(projectId: string): Promise<ApiResult<OutlinePayload | undefined>> {
  return withFallback(() => request<OutlinePayload>(`/projects/${projectId}/outline`), undefined);
}

export function rebuildDependencies(projectId: string, outlineId: string, contentHash: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(() => request<Job>(`/projects/${projectId}/outline/dependencies/rebuild`, {
    method: 'POST', body: JSON.stringify({ outline_id: outlineId, content_hash: contentHash }),
  }), undefined);
}

export function generateOutline(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/outline/generate`, { method: 'POST' }),
    undefined,
  );
}

export function rebuildDraft(
  projectId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/draft/rebuild`, { method: 'POST' }),
    undefined,
  );
}

export function updateOutline(
  projectId: string,
  tree: OutlineTree,
  status: 'draft' | 'confirmed' = 'draft',
): Promise<ApiResult<OutlinePayload | undefined>> {
  return withFallback(
    () =>
      request<OutlinePayload>(`/projects/${projectId}/outline`, {
        method: 'PUT',
        body: JSON.stringify({ tree, status }),
      }),
    undefined,
  );
}

export function generateSections(
  projectId: string,
  coherence = true,
  polishPolicy?: import("./types").PolishPolicy,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/sections/generate`, {
        method: 'POST',
        body: JSON.stringify({ coherence, polish_policy: polishPolicy }),
      }),
    undefined,
  );
}

export function generateAll(
  projectId: string,
  options: { quality_profile?: QualityProfile; review_style?: ReviewStyle } = {},
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/generate`, {
        method: 'POST',
        body: JSON.stringify(options),
      }),
    undefined,
  );
}

export function listSections(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<PaperSection[]>> {
  return withFallback(
    () => request<PaperSection[]>(`/projects/${projectId}/sections`, { signal }),
    [],
  );
}

/**
 * 保存章节。
 *
 * `expectedUpdatedAt` 是乐观并发的基础版本：带上它，服务端发现章节已被别处
 * 改动（最典型的是视觉批准刚把 FigureBlock 写进这一节）就返回 409
 * `section_changed`，而不是让这次保存把插图静默删掉。
 */
export function updateSection(
  projectId: string,
  sectionKey: string,
  bodyIr: SectionIR,
  title?: string,
  expectedUpdatedAt?: string | null,
): Promise<ApiResult<PaperSection | undefined>> {
  return withFallback(
    () =>
      request<PaperSection>(`/projects/${projectId}/sections/${sectionKey}`, {
        method: 'PUT',
        body: JSON.stringify({
          body_ir: bodyIr,
          title,
          expected_updated_at: expectedUpdatedAt ?? undefined,
        }),
      }),
    undefined,
  );
}

export function getCitationAudit(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<CitationAudit | undefined>> {
  return withFallback(
    () => request<CitationAudit>(`/projects/${projectId}/citations/audit`, { signal }),
    undefined,
  );
}

export function getMarkdownPreview(
  projectId: string,
): Promise<ApiResult<MarkdownPreview | undefined>> {
  return withFallback(
    () => request<MarkdownPreview>(`/projects/${projectId}/preview/markdown`),
    undefined,
  );
}

// ---- 导出中心（M3） ----

/** 全部可请求的导出格式。docx 此前不在默认值里，导致界面永远触发不到它。 */
export const ALL_EXPORT_FORMATS: RequestableExportFormat[] = [
  'pdf',
  'latex_zip',
  'markdown',
  'markdown_bundle',
  'bibtex',
  'docx',
];

export function startExport(
  projectId: string,
  formats?: RequestableExportFormat[],
  qualityProfile: QualityProfile = 'scholarly',
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/exports`, {
        method: 'POST',
        body: JSON.stringify({
          formats: formats ?? ALL_EXPORT_FORMATS,
          quality_profile: qualityProfile,
        }),
      }),
    undefined,
  );
}

export function listExports(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<ExportArtifact[]>> {
  return withFallback(
    () => request<ExportArtifact[]>(`/projects/${projectId}/exports`, { signal }),
    [],
  );
}

/** 产物下载直链（浏览器直接跳转，不经过 fetch）。 */
export function exportDownloadUrl(projectId: string, artifactId: string): string {
  return `${API_BASE}${API_PREFIX}/projects/${projectId}/exports/${artifactId}/download`;
}

/** 一个真实导出任务下全部可用文件的 zip 下载直链。 */
export function exportRunDownloadUrl(projectId: string, runId: string): string {
  return `${API_BASE}${API_PREFIX}/projects/${projectId}/exports/runs/${runId}/download`;
}

/** 使用失败批次中持久化的格式、质量档位与正文快照参数重新导出。 */
export function retryExportRun(projectId: string, runId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/exports/runs/${runId}/retry`, {
    method: 'POST',
  });
}

/**
 * 页内预览直链（`Content-Disposition: inline`）。
 *
 * 预览必须与下载分开：把 attachment 直链塞进 `<iframe src>`，浏览器会当成
 * 一次下载——「打开导出中心」于是等于「凭空下载一个 PDF」，而预览框始终空白。
 */
export function exportPreviewUrl(projectId: string, artifactId: string): string {
  return `${exportDownloadUrl(projectId, artifactId)}?disposition=inline`;
}

// ---- 素材中心（M4） ----

export function listAssets(projectId: string, signal?: AbortSignal): Promise<ApiResult<UserAsset[]>> {
  return withFallback(
    () => request<UserAsset[]>(`/projects/${projectId}/assets`, { signal }),
    [],
  );
}

/** 上传素材：后端确定性解析入 parsed_json（正文数字的唯一合法出处）。 */
export async function uploadAsset(
  projectId: string,
  file: File,
  kind?: string,
): Promise<ApiResult<UserAsset | undefined>> {
  const form = new FormData();
  form.append('file', file);
  if (kind) form.append('kind', kind);
  try {
    const res = await fetch(`${API_BASE}${API_PREFIX}/projects/${projectId}/assets`, {
      method: 'POST',
      body: form,
      credentials: 'include',
    });
    if (!res.ok) {
      if (res.status === 401) notifyAuthenticationFailure();
      if (DEMO_MODE && (res.status === 501 || res.status === 404)) {
        return { data: undefined, source: 'mock', note: `后端端点未实现（${res.status}）` };
      }
      throw new Error(`API ${res.status}: ${await res.text().catch(() => res.statusText)}`);
    }
    return { data: (await res.json()) as UserAsset, source: 'live' };
  } catch (err) {
    if (err instanceof Error && err.message.startsWith('API ')) throw err;
    if (DEMO_MODE) return { data: undefined, source: 'mock', note: '无法连接后端 API' };
    throw err;
  }
}

export function deleteAsset(projectId: string, assetId: string): Promise<ApiResult<void>> {
  return withFallback(
    () => request<void>(`/projects/${projectId}/assets/${assetId}`, { method: 'DELETE' }),
    undefined as unknown as void,
  );
}

/** 素材原文件下载直链（后端 GET /assets/{id}/download 一直存在，此前无 UI 入口）。 */
export function assetDownloadUrl(projectId: string, assetId: string): string {
  return `${API_BASE}${API_PREFIX}/projects/${projectId}/assets/${assetId}/download`;
}

// ---- 图表与插图（M8） ----

export function listVisuals(projectId: string): Promise<ApiResult<VisualAsset[]>> {
  return withFallback(
    () =>
      request<VisualAsset[]>(`/projects/${projectId}/visuals`).then((rows) =>
        rows.map((visual) => ({
          ...visual,
          renditions: Object.fromEntries(
            Object.entries(visual.renditions).map(([format, item]) => [
              format,
              item
                ? {
                    ...item,
                    url: item.url.startsWith('http') ? item.url : `${API_BASE}${item.url}`,
                  }
                : item,
            ]),
          ) as VisualAsset['renditions'],
        })),
      ),
    [],
  );
}

/**
 * 视觉状态计数。
 *
 * 导航状态点、项目概览与导出提醒只要这几个数字；让它们各自拉一遍完整视觉
 * 列表（含 spec 与 renditions）纯属浪费。降级值全 0——摘要拿不到不该让导航消失。
 */
export function getVisualSummary(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<VisualSummary>> {
  return withFallback(
    () => request<VisualSummary>(`/projects/${projectId}/visuals/summary`, { signal }),
    {
      project_id: projectId,
      pending: 0,
      generating: 0,
      ready: 0,
      approved: 0,
      failed: 0,
      rejected: 0,
      stale: 0,
      latest_approved_at: null,
    },
  );
}

export function suggestVisuals(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/visuals/suggest`, { method: 'POST' }),
    undefined,
  );
}

/**
 * 让模型把「一句话想法」补成完整规格。
 *
 * 只回草稿：不落库、不排任务、不调用图像服务。用户看过再点创建。
 */
export function draftVisual(
  projectId: string,
  payload: {
    kind: 'auto' | 'chart' | 'diagram' | 'ai_image';
    intent: string;
    target_section_key?: string | null;
    source_asset_refs?: string[];
  },
): Promise<ApiResult<VisualDraft | undefined>> {
  return withFallback(
    () =>
      request<VisualDraft>(`/projects/${projectId}/visuals/draft`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    undefined,
  );
}

export function createVisual(
  projectId: string,
  payload: CreateVisualRequest,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    undefined,
  );
}

export function updateVisual(
  projectId: string,
  visualId: string,
  payload: Partial<CreateVisualRequest>,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}`, {
        method: 'PATCH',
        body: JSON.stringify(payload),
      }),
    undefined,
  );
}

export function generateVisual(
  projectId: string,
  visualId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/visuals/${visualId}/generate`, {
        method: 'POST',
      }),
    undefined,
  );
}

/**
 * 在付费确认框出现前，让模型读取当前论文全文并生成最终生图提示词。
 * 已经绑定当前正文快照的提示词由后端直接复用；旧草稿或正文变化后会重新分析。
 */
export function prepareVisualGeneration(
  projectId: string,
  visualId: string,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}/prepare-generation`, {
        method: 'POST',
      }),
    undefined,
  );
}

/**
 * 批准并插入。
 *
 * 与 `updateSection` 共用同一套乐观并发协议：`expectedSectionUpdatedAt` 让
 * 服务端在目标章节已被改动时返回 409，而不是把插图写进一份过期的 IR。
 */
export function approveVisual(
  projectId: string,
  visualId: string,
  sectionKey: string,
  blockIndex: number,
  expectedSectionUpdatedAt?: string | null,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}/approve`, {
        method: 'POST',
        body: JSON.stringify({
          section_key: sectionKey,
          block_index: blockIndex,
          expected_section_updated_at: expectedSectionUpdatedAt ?? undefined,
        }),
      }),
    undefined,
  );
}

export function rejectVisual(
  projectId: string,
  visualId: string,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}/reject`, {
        method: 'POST',
      }),
    undefined,
  );
}

export function regenerateVisual(
  projectId: string,
  visualId: string,
  payload: RegenerateVisualRequest = {},
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}/regenerate`, {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    undefined,
  );
}

export function visualRenditionUrl(
  projectId: string,
  visualId: string,
  format: 'svg' | 'pdf' | 'png' = 'png',
): string {
  return `${API_BASE}${API_PREFIX}/projects/${projectId}/visuals/${visualId}/renditions/${format}`;
}

export function getNumLint(projectId: string, signal?: AbortSignal): Promise<ApiResult<NumLintReport | undefined>> {
  return withFallback(() => request<NumLintReport>(`/projects/${projectId}/numlint`, { signal }), undefined);
}

// ---- 雪球 / 全文 / 质量 / 润色（M5） ----

export function startSnowball(
  projectId: string,
  direction: 'both' | 'forward' | 'backward' = 'both',
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/snowball`, {
        method: 'POST',
        body: JSON.stringify({ direction, max_seeds: 8 }),
      }),
    undefined,
  );
}

export function startIngest(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/ingest`, {
        method: 'POST',
        body: JSON.stringify({ max_works: 12 }),
      }),
    undefined,
  );
}

export function getQuality(
  projectId: string,
  qualityProfile: QualityProfile = 'scholarly',
  signal?: AbortSignal,
): Promise<ApiResult<QualityReport | undefined>> {
  return withFallback(
    () =>
      request<QualityReport>(
        `/projects/${projectId}/quality?quality_profile=${qualityProfile}`,
        { signal },
      ),
    undefined,
  );
}

export function generateQuality(
  projectId: string,
  options: { quality_profile?: QualityProfile; review_style?: ReviewStyle } = {},
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/quality/generate`, {
        method: 'POST',
        body: JSON.stringify(options),
      }),
    undefined,
  );
}

export function repairQuality(
  projectId: string,
  options: { quality_profile?: QualityProfile; review_style?: ReviewStyle } = {},
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/quality/repair`, {
        method: 'POST',
        body: JSON.stringify(options),
      }),
    undefined,
  );
}

export function getClaimEvidence(
  projectId: string,
  reportId?: string,
  coreOnly = false,
): Promise<ApiResult<ClaimEvidence[]>> {
  const params = new URLSearchParams();
  if (reportId) params.set('report_id', reportId);
  if (coreOnly) params.set('core_only', 'true');
  const query = params.size ? `?${params.toString()}` : '';
  return withFallback(
    () => request<ClaimEvidence[]>(`/projects/${projectId}/quality/evidence${query}`),
    [],
  );
}

export function getResearchQuestions(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<ResearchQuestion[]>> {
  return withFallback(
    () => request<ResearchQuestion[]>(`/projects/${projectId}/questions`, { signal }),
    [],
  );
}

export function generateResearchQuestions(
  projectId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/questions/generate`, {
        method: 'POST',
      }),
    undefined,
  );
}

export function updateResearchQuestion(
  projectId: string,
  questionId: string,
  changes: Partial<
    Pick<
      ResearchQuestion,
      | 'text'
      | 'comparison_dimensions'
      | 'expected_evidence_kinds'
      | 'answer_status'
      | 'search_query'
      | 'locked'
    >
  >,
): Promise<ResearchQuestion> {
  return request<ResearchQuestion>(`/projects/${projectId}/questions/${questionId}`, {
    method: 'PATCH',
    body: JSON.stringify(changes),
  });
}

export function getEligibilityDecisions(
  projectId: string,
  decision?: 'include' | 'exclude' | 'uncertain',
): Promise<ApiResult<EligibilityDecision[]>> {
  const query = decision ? `?decision=${decision}` : '';
  return withFallback(
    () =>
      request<EligibilityDecision[]>(
        `/projects/${projectId}/library/eligibility-decisions${query}`,
      ),
    [],
  );
}

export function getEvidenceUnits(projectId: string): Promise<ApiResult<EvidenceUnit[]>> {
  return withFallback(
    () => request<EvidenceUnit[]>(`/projects/${projectId}/evidence-units`),
    [],
  );
}

export function generateEvidenceUnits(
  projectId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/evidence-units/generate`, {
        method: 'POST',
      }),
    undefined,
  );
}

export function getEvidenceMatrix(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<EvidenceMatrix | undefined>> {
  return withFallback(
    () => request<EvidenceMatrix>(`/projects/${projectId}/evidence-matrix`, { signal }),
    undefined,
  );
}

export function generateEvidenceMatrix(
  projectId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/evidence-matrix/generate`, {
        method: 'POST',
      }),
    undefined,
  );
}

export function generateSynthesis(
  projectId: string,
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/synthesis/generate`, {
        method: 'POST',
      }),
    undefined,
  );
}

export function getSynthesis(
  projectId: string,
): Promise<ApiResult<SynthesisPayload | undefined>> {
  return withFallback(
    () => request<SynthesisPayload>(`/projects/${projectId}/synthesis`),
    undefined,
  );
}

export function updateEvidenceMatrixLink(
  projectId: string,
  linkId: string,
  stance: EvidenceStance,
  conditionNote?: string | null,
): Promise<EvidenceMatrix['links'][number]> {
  return request<EvidenceMatrix['links'][number]>(
    `/projects/${projectId}/evidence-matrix/${linkId}`,
    {
      method: 'PATCH',
      body: JSON.stringify({ stance, condition_note: conditionNote }),
    },
  );
}

export function reviewClaimEvidence(
  projectId: string,
  anchorId: string,
  manualStatus: ClaimEvidence['manual_status'],
): Promise<ClaimEvidence> {
  return request<ClaimEvidence>(`/projects/${projectId}/quality/evidence/${anchorId}`, {
    method: 'PATCH',
    body: JSON.stringify({ manual_status: manualStatus }),
  });
}

/** 润色浮条：绝不新增引用、绝不改动数字（服务端二次把关）。 */
export function refineText(
  projectId: string,
  sectionKey: string,
  action: RefineAction,
  text: string,
): Promise<ApiResult<RefineResult | undefined>> {
  return withFallback(
    () =>
      request<RefineResult>(`/projects/${projectId}/sections/${sectionKey}/refine`, {
        method: 'POST',
        body: JSON.stringify({ action, text }),
      }),
    undefined,
  );
}

// ---- 设置 / 成本 / 版本历史（M6） ----

export function getRuntimeSettings(): Promise<ApiResult<RuntimeSettings | undefined>> {
  return withFallback(() => request<RuntimeSettings>('/settings'), undefined);
}

export function getCostDetail(projectId: string, signal?: AbortSignal): Promise<ApiResult<CostDetail | undefined>> {
  return withFallback(() => request<CostDetail>(`/projects/${projectId}/cost/detail`, { signal }), undefined);
}

export function getVersionHistory(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<VersionHistory | undefined>> {
  return withFallback(() => request<VersionHistory>(`/projects/${projectId}/versions`, { signal }), undefined);
}

export function getSubmissionReadiness(
  projectId: string,
  signal?: AbortSignal,
): Promise<ApiResult<SubmissionReadiness | undefined>> {
  return withFallback(
    () => request<SubmissionReadiness>(`/projects/${projectId}/readiness`, { signal }),
    undefined,
  );
}

/** 把历史文稿复制为一个新的当前版本；旧版本永不原地覆盖。 */
export function restoreDocumentVersion(
  projectId: string,
  documentId: string,
): Promise<DocumentVersion> {
  return request<DocumentVersion>(`/projects/${projectId}/versions/${documentId}/restore`, {
    method: 'POST',
  });
}

export function getAgentTrace(projectId: string, jobId: string, signal?: AbortSignal) {
  return request<import('./types').AgentTrace>(`/projects/${projectId}/jobs/${jobId}/trace`, { signal });
}

export function searchEvidence(projectId: string, query: string, signal?: AbortSignal) {
  return request<import('./types').EvidenceSearch>(
    `/projects/${projectId}/evidence/search?q=${encodeURIComponent(query)}&mode=hybrid_rerank`, { signal });
}

export function indexEvidence(projectId: string) {
  return request<Job>(`/projects/${projectId}/evidence/index`, { method: 'POST' });
}

export function respondToRepair(projectId: string, jobId: string,
  response: { interrupt_id: string; choice: 'continue' | 'finish' }) {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/resume`, {
    method: 'POST', body: JSON.stringify(response),
  });
}
