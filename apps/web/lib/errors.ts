/** 把抛出来的东西翻成用户能看懂的一句中文。 */
export function describeError(err: unknown): string {
  if (err instanceof Error) {
    const match = /^API (\d{3}):\s*([\s\S]*)$/.exec(err.message);
    if (match) {
      const status = Number(match[1]);
      const detail = match[2]?.trim();
      const base = HTTP_HINT[status] ?? `后端返回 ${status}`;
      return detail ? `${base}（${truncate(detail)}）` : base;
    }
    if (err.message.includes('Failed to fetch') || err.message.includes('NetworkError')) {
      return networkHelp();
    }
    return err.message || '发生了未知错误。';
  }
  // 浏览器代理、跨 realm Error 和部分 fetch/polyfill 会抛出“长得像 Error”
  // 的普通对象。不能因为 instanceof 失效就把真实原因抹成“未知错误”。
  if (typeof err === 'string' && err.trim()) return err.trim();
  if (err && typeof err === 'object') {
    const candidate = err as {
      message?: unknown;
      status?: unknown;
      detail?: unknown;
      code?: unknown;
    };
    const message = typeof candidate.message === 'string' ? candidate.message.trim() : '';
    if (message) return message;
    const detail =
      typeof candidate.detail === 'string'
        ? candidate.detail.trim()
        : candidate.detail && typeof candidate.detail === 'object'
          ? JSON.stringify(candidate.detail)
          : '';
    if (detail) return truncate(detail);
    if (typeof candidate.status === 'number') {
      return HTTP_HINT[candidate.status] ?? `后端返回 ${candidate.status}`;
    }
    if (typeof candidate.code === 'string' && candidate.code.trim()) {
      return `请求失败（${candidate.code.trim()}）`;
    }
  }
  return '发生了未知错误。';
}

/** 生产界面不泄露仓库命令和本机路径。 */
export function networkHelp(): string {
  return process.env.NODE_ENV === 'production'
    ? '服务暂时不可用，请稍后重试或联系管理员。'
    : '无法连接后端 API，请确认 ./scripts/dev up 已启动。';
}

/**
 * 是否是章节乐观并发冲突（后端 409 `section_changed`）。
 *
 * 这类失败与「保存挂了」性质完全不同：草稿完好无损，用户需要的是先看服务端的
 * 新版本再合并，而不是重试。界面必须区分这两种，否则用户会反复点保存，最终
 * 把别处刚插入的图覆盖掉。
 */
export function isSectionChanged(err: unknown): boolean {
  return err instanceof Error && /^API 409:/.test(err.message) && err.message.includes('section_changed');
}

const HTTP_HINT: Record<number, string> = {
  400: '请求参数有误',
  401: '未授权',
  403: '没有权限',
  409: '与当前状态冲突，请刷新后重试',
  413: '文件过大，后端拒绝接收',
  422: '数据校验未通过',
  429: '请求过于频繁，请稍后再试',
  500: '后端内部错误',
  502: '后端网关错误，服务可能正在重启',
  503: '后端暂时不可用',
  504: '后端响应超时',
};

function truncate(text: string, max = 160): string {
  const clean = text.replace(/\s+/g, ' ').trim();
  return clean.length > max ? `${clean.slice(0, max)}…` : clean;
}
