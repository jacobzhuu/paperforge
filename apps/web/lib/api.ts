import type {
  ApiResult,
  CreateProjectRequest,
  Job,
  JobEvent,
  LibraryEntry,
  LibraryEntryStatus,
  Project,
  CitationAudit,
  CostDetail,
  ExportArtifact,
  RequestableExportFormat,
  MarkdownPreview,
  NumLintReport,
  OutlinePayload,
  OutlineTree,
  PaperSection,
  ProjectCost,
  QualityReport,
  RefineAction,
  RefineResult,
  ScopePayload,
  SearchRun,
  RuntimeSettings,
  SectionIR,
  UpdateProjectRequest,
  UserAsset,
  VersionHistory,
  VisualAsset,
  CreateVisualRequest,
  AuthUser,
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
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
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
    throw new ApiError(res.status, code, message);
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

export async function getProject(id: string): Promise<ApiResult<Project | undefined>> {
  return withFallback(
    () => request<Project>(`/projects/${id}`),
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

// ---- Library ----

export function listLibrary(
  projectId: string,
  status?: string,
): Promise<ApiResult<LibraryEntry[]>> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : '';
  return withFallback(
    () => request<LibraryEntry[]>(`/projects/${projectId}/library${qs}`),
    MOCK_LIBRARY,
  );
}

export function listSearchRuns(projectId: string): Promise<ApiResult<SearchRun[]>> {
  return withFallback(
    () => request<SearchRun[]>(`/projects/${projectId}/search/runs`),
    MOCK_SEARCH_RUNS,
  );
}

// ---- Scope ----

export function getScope(projectId: string): Promise<ApiResult<ScopePayload | undefined>> {
  return withFallback(
    () => request<{ project_id: string; scope: ScopePayload }>(`/projects/${projectId}/scope`)
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

export function listJobs(projectId: string): Promise<ApiResult<Job[]>> {
  return withFallback(() => request<Job[]>(`/projects/${projectId}/jobs`), []);
}

/** 重连退避（毫秒）；耗尽后转入 getJob 轮询。 */
const SSE_RETRY_DELAYS = [1000, 2000, 4000, 8000];
const JOB_POLL_INTERVAL = 3000;
const TERMINAL_JOB_STATUSES = new Set(['succeeded', 'failed', 'cancelled']);

export interface JobStreamHandlers {
  onEvent?: (event: JobEvent) => void;
  /** 任务真正结束（收到 job.closed 或轮询到终态）。 */
  onClose?: () => void;
  /** 连接中断中：reconnecting=true 表示正在重试，false 表示已恢复。 */
  onConnectionChange?: (state: { reconnecting: boolean; degradedToPolling: boolean }) => void;
}

/**
 * 订阅任务进度（SSE），断线自动重连，重连耗尽后回落到 getJob 轮询。返回取消函数。
 *
 * 后端以无名事件下发（见 services/api/.../events.py::_format_event），因此这里只需
 * onmessage 一个入口——不再有「白名单漏了某个阶段 ⇒ 进度整段静默丢失」的可能。
 *
 * onerror 绝不能等同于 onClose：休眠、代理空闲超时、CORS 抖动都会触发 onerror，
 * 而任务在服务端仍在跑。把它当成功收尾会让十分钟的检索「看起来完成了」。
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
  let attempt = 0;
  let lastSeq = 0;
  let stopped = false;

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
  };

  /** 重连耗尽后的兜底：轮询任务本身，直到终态。 */
  const startPolling = () => {
    if (pollTimer || stopped) return;
    handlers.onConnectionChange?.({ reconnecting: true, degradedToPolling: true });
    pollTimer = setInterval(() => {
      void getJob(projectId, jobId)
        .then(({ data }) => {
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
        })
        .catch(() => {
          /* 轮询失败继续下一轮 */
        });
    }, JOB_POLL_INTERVAL);
  };

  const connect = () => {
    if (stopped) return;
    const url = lastSeq > 0 ? `${baseUrl}?after=${lastSeq}` : baseUrl;
    const es = new EventSource(url, { withCredentials: true });
    source = es;

    es.onopen = () => {
      attempt = 0;
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
        startPolling();
      }
    };
  };

  connect();

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

/** 从文献库移除条目（后端 DELETE /library/entries/{id} 一直存在，此前无 UI 入口）。 */
export function deleteLibraryEntry(projectId: string, entryId: string): Promise<ApiResult<void>> {
  return withFallback(
    () =>
      request<void>(`/projects/${projectId}/library/entries/${entryId}`, { method: 'DELETE' }),
    undefined as unknown as void,
  );
}

/** R1 写作白名单：引用 chip 的唯一取值来源。 */
export function getWhitelist(projectId: string): Promise<ApiResult<string[]>> {
  return withFallback(
    () =>
      request<{ project_id: string; cite_keys: string[] }>(
        `/projects/${projectId}/library/whitelist`,
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

export function generateOutline(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/outline/generate`, { method: 'POST' }),
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
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/sections/generate`, {
        method: 'POST',
        body: JSON.stringify({ coherence }),
      }),
    undefined,
  );
}

export function generateAll(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/generate`, { method: 'POST' }),
    undefined,
  );
}

export function listSections(projectId: string): Promise<ApiResult<PaperSection[]>> {
  return withFallback(() => request<PaperSection[]>(`/projects/${projectId}/sections`), []);
}

export function updateSection(
  projectId: string,
  sectionKey: string,
  bodyIr: SectionIR,
  title?: string,
): Promise<ApiResult<PaperSection | undefined>> {
  return withFallback(
    () =>
      request<PaperSection>(`/projects/${projectId}/sections/${sectionKey}`, {
        method: 'PUT',
        body: JSON.stringify({ body_ir: bodyIr, title }),
      }),
    undefined,
  );
}

export function getCitationAudit(
  projectId: string,
): Promise<ApiResult<CitationAudit | undefined>> {
  return withFallback(
    () => request<CitationAudit>(`/projects/${projectId}/citations/audit`),
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
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/exports`, {
        method: 'POST',
        body: JSON.stringify({ formats: formats ?? ALL_EXPORT_FORMATS }),
      }),
    undefined,
  );
}

export function listExports(projectId: string): Promise<ApiResult<ExportArtifact[]>> {
  return withFallback(() => request<ExportArtifact[]>(`/projects/${projectId}/exports`), []);
}

/** 产物下载直链（浏览器直接跳转，不经过 fetch）。 */
export function exportDownloadUrl(projectId: string, artifactId: string): string {
  return `${API_BASE}${API_PREFIX}/projects/${projectId}/exports/${artifactId}/download`;
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

export function listAssets(projectId: string): Promise<ApiResult<UserAsset[]>> {
  return withFallback(() => request<UserAsset[]>(`/projects/${projectId}/assets`), []);
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

export function suggestVisuals(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/visuals/suggest`, { method: 'POST' }),
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

export function approveVisual(
  projectId: string,
  visualId: string,
  sectionKey: string,
  blockIndex: number,
): Promise<ApiResult<VisualAsset | undefined>> {
  return withFallback(
    () =>
      request<VisualAsset>(`/projects/${projectId}/visuals/${visualId}/approve`, {
        method: 'POST',
        body: JSON.stringify({ section_key: sectionKey, block_index: blockIndex }),
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
  payload: Partial<CreateVisualRequest> = {},
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

export function getNumLint(projectId: string): Promise<ApiResult<NumLintReport | undefined>> {
  return withFallback(() => request<NumLintReport>(`/projects/${projectId}/numlint`), undefined);
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

export function getQuality(projectId: string): Promise<ApiResult<QualityReport | undefined>> {
  return withFallback(() => request<QualityReport>(`/projects/${projectId}/quality`), undefined);
}

export function generateQuality(projectId: string): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () => request<Job>(`/projects/${projectId}/quality/generate`, { method: 'POST' }),
    undefined,
  );
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

export function getCostDetail(projectId: string): Promise<ApiResult<CostDetail | undefined>> {
  return withFallback(() => request<CostDetail>(`/projects/${projectId}/cost/detail`), undefined);
}

export function getVersionHistory(
  projectId: string,
): Promise<ApiResult<VersionHistory | undefined>> {
  return withFallback(() => request<VersionHistory>(`/projects/${projectId}/versions`), undefined);
}
