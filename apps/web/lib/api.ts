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
  ExportFormat,
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
  UserAsset,
  VersionHistory,
} from './types';
import { JOB_EVENT_TYPES } from './types';
import { MOCK_LIBRARY, MOCK_PROJECTS, MOCK_SEARCH_RUNS } from './mock';

// API 基址：默认指向本地 API（scripts/dev up 暴露 8080）。可用环境变量覆盖。
export const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, '') ?? 'http://localhost:8080';

const API_PREFIX = '/api/v1';

class DegradeError extends Error {
  constructor(public reason: string) {
    super(reason);
  }
}

/**
 * 统一的带降级请求：
 * - 网络错误 / 后端未实现(501) / 404 时，抛出 DegradeError，由调用方回退到示例数据；
 * - 其他非 2xx 抛出普通 Error，交由调用方决定是否提示。
 */
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
      cache: 'no-store',
    });
  } catch {
    throw new DegradeError('无法连接后端 API');
  }
  if (res.status === 501 || res.status === 404) {
    throw new DegradeError(`后端端点未实现（${res.status}）`);
  }
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(`API ${res.status}: ${text || res.statusText}`);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

async function withFallback<T>(
  live: () => Promise<T>,
  fallback: T,
  emptyIsMock = false,
): Promise<ApiResult<T>> {
  try {
    const data = await live();
    // 后端骨架期返回空数组时，用示例数据填充以便预览界面。
    if (emptyIsMock && Array.isArray(data) && data.length === 0) {
      return { data: fallback, source: 'mock', note: '后端返回空，展示示例数据' };
    }
    return { data, source: 'live' };
  } catch (err) {
    if (err instanceof DegradeError) {
      return { data: fallback, source: 'mock', note: err.reason };
    }
    throw err;
  }
}

// ---- Projects ----

export function listProjects(): Promise<ApiResult<Project[]>> {
  return withFallback(() => request<Project[]>('/projects'), MOCK_PROJECTS, true);
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

// ---- Library ----

export function listLibrary(
  projectId: string,
  status?: string,
): Promise<ApiResult<LibraryEntry[]>> {
  const qs = status ? `?status=${encodeURIComponent(status)}` : '';
  return withFallback(
    () => request<LibraryEntry[]>(`/projects/${projectId}/library${qs}`),
    MOCK_LIBRARY,
    true,
  );
}

export function listSearchRuns(projectId: string): Promise<ApiResult<SearchRun[]>> {
  return withFallback(
    () => request<SearchRun[]>(`/projects/${projectId}/search/runs`),
    MOCK_SEARCH_RUNS,
    true,
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
 * 订阅任务进度（SSE）。返回取消函数。
 * 后端不可用时不抛错，仅不产生事件——调用方另有轮询兜底。
 */
export function subscribeJobEvents(
  projectId: string,
  jobId: string,
  handlers: { onEvent?: (event: JobEvent) => void; onClose?: () => void },
): () => void {
  if (typeof window === 'undefined' || typeof EventSource === 'undefined') return () => {};
  const url = `${API_BASE}${API_PREFIX}/projects/${projectId}/jobs/${jobId}/events`;
  const source = new EventSource(url);
  const handle = (raw: MessageEvent) => {
    try {
      handlers.onEvent?.(JSON.parse(raw.data) as JobEvent);
    } catch {
      /* 事件解析失败不应打断进度流 */
    }
  };
  source.onmessage = handle;
  for (const type of JOB_EVENT_TYPES) source.addEventListener(type, handle as EventListener);
  source.addEventListener('job.closed', ((raw: MessageEvent) => {
    handle(raw);
    source.close();
    handlers.onClose?.();
  }) as EventListener);
  source.onerror = () => {
    source.close();
    handlers.onClose?.();
  };
  return () => source.close();
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

export function startExport(
  projectId: string,
  formats?: ExportFormat[],
): Promise<ApiResult<Job | undefined>> {
  return withFallback(
    () =>
      request<Job>(`/projects/${projectId}/exports`, {
        method: 'POST',
        body: JSON.stringify({ formats: formats ?? ['pdf', 'latex_zip', 'markdown', 'bibtex'] }),
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
    });
    if (!res.ok) {
      if (res.status === 501 || res.status === 404) {
        return { data: undefined, source: 'mock', note: `后端端点未实现（${res.status}）` };
      }
      throw new Error(`API ${res.status}: ${await res.text().catch(() => res.statusText)}`);
    }
    return { data: (await res.json()) as UserAsset, source: 'live' };
  } catch (err) {
    if (err instanceof Error && err.message.startsWith('API ')) throw err;
    return { data: undefined, source: 'mock', note: '无法连接后端 API' };
  }
}

export function deleteAsset(projectId: string, assetId: string): Promise<ApiResult<void>> {
  return withFallback(
    () => request<void>(`/projects/${projectId}/assets/${assetId}`, { method: 'DELETE' }),
    undefined as unknown as void,
  );
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
