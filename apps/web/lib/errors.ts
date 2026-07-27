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
      return '无法连接后端 API，请确认 ./scripts/dev up 已启动。';
    }
    return err.message || '发生了未知错误。';
  }
  return '发生了未知错误。';
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
