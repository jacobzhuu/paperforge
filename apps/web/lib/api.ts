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
  QualityProfile,
  ReviewStyle,
  ClaimEvidence,
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
  VisualDraft,
  VisualSummary,
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

/**
 * 跳过剩余的连贯性润色。不走 withFallback：这是用户的一次明确指令，
 * 后端不可用时必须报错，而不是静悄悄地"看起来成功了"。
 */
export function skipPolish(projectId: string, jobId: string): Promise<Job> {
  return request<Job>(`/projects/${projectId}/jobs/${jobId}/polish/skip`, { method: 'POST' });
}

/** 重连退避（毫秒）；任务运行期间另有 getJob 周期校准兜底。 */
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
  let pollInFlight = false;
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
    if (degraded) {
      handlers.onConnectionChange?.({ reconnecting: true, degradedToPolling: true });
    }
    if (pollTimer) return;
    pollTimer = setInterval(() => void pollJob(), JOB_POLL_INTERVAL);
  };

  const connect = () => {
    if (stopped) return;
    const url = lastSeq > 0 ? `${baseUrl}?after=${lastSeq}` : baseUrl;
    const es = new EventSource(url, { withCredentials: true });
    source = es;

    es.onopen = () => {
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

export function listSections(projectId: string): Promise<ApiResult<PaperSection[]>> {
  return withFallback(() => request<PaperSection[]>(`/projects/${projectId}/sections`), []);
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
  qualityProfile: QualityProfile = 'draft',
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

/**
 * 视觉状态计数。
 *
 * 导航状态点、项目概览与导出提醒只要这几个数字；让它们各自拉一遍完整视觉
 * 列表（含 spec 与 renditions）纯属浪费。降级值全 0——摘要拿不到不该让导航消失。
 */
export function getVisualSummary(projectId: string): Promise<ApiResult<VisualSummary>> {
  return withFallback(
    () => request<VisualSummary>(`/projects/${projectId}/visuals/summary`),
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
  payload: { kind: 'diagram' | 'ai_image'; intent: string; target_section_key?: string | null },
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

export function getQuality(
  projectId: string,
  qualityProfile: QualityProfile = 'draft',
): Promise<ApiResult<QualityReport | undefined>> {
  return withFallback(
    () =>
      request<QualityReport>(
        `/projects/${projectId}/quality?quality_profile=${qualityProfile}`,
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

export function getCostDetail(projectId: string): Promise<ApiResult<CostDetail | undefined>> {
  return withFallback(() => request<CostDetail>(`/projects/${projectId}/cost/detail`), undefined);
}

export function getVersionHistory(
  projectId: string,
): Promise<ApiResult<VersionHistory | undefined>> {
  return withFallback(() => request<VersionHistory>(`/projects/${projectId}/versions`), undefined);
}
