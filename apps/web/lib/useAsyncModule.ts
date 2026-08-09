'use client';

import * as React from 'react';
import { describeError } from './errors';

/**
 * 模块级独立加载。
 *
 * 取代各工作台里「一个 `Promise.all` 拉齐所有数据」的写法。那种写法把互不相干的
 * 接口绑成一个原子：素材页的 NUMLINT 超时会让原始素材、视觉资产、运行时设置
 * 一起显示加载失败；写作台更严重——七个请求里任意一个失败，正文编辑器本身
 * 就打不开，哪怕 `listSections` 早就成功返回了。
 *
 * 这里每个数据模块持有自己的 loading / error / reload，互不牵连：
 *
 *   - 失败的模块显示自己的重试按钮，其余模块照常渲染；
 *   - 依赖变化（典型是切换项目）时旧请求的结果被丢弃，不会把上一个项目的数据
 *     写进新项目的界面；
 *   - `fallback` 保证 data 永远可用，调用方不必到处判空。
 */
export interface AsyncModule<T> {
  data: T;
  loading: boolean;
  error: string | null;
  /** 至少成功加载过一次。用于区分「首次加载中」与「刷新中」。 */
  ready: boolean;
  reload: () => void;
}

export function useAsyncModule<T>(
  load: (signal: AbortSignal) => Promise<T>,
  fallback: T,
  deps: React.DependencyList,
): AsyncModule<T> {
  const [data, setData] = React.useState<T>(fallback);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [ready, setReady] = React.useState(false);
  const [token, setToken] = React.useState(0);

  // load 通常是内联箭头函数，每次渲染都是新引用；放进 ref 后由 deps 决定何时重跑，
  // 否则这个 effect 每渲染一次就重新发一轮请求。
  const loadRef = React.useRef(load);
  React.useEffect(() => {
    loadRef.current = load;
  });

  React.useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    loadRef
      .current(controller.signal)
      .then((value) => {
        if (controller.signal.aborted) return;
        setData(value);
        setReady(true);
        setLoading(false);
      })
      .catch((err) => {
        if (controller.signal.aborted) return;
        setError(describeError(err));
        setLoading(false);
      });
    return () => {
      // 切项目 / 主动重载时真正终止 fetch，避免迟到响应和无效网络占用。
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, token]);

  const reload = React.useCallback(() => setToken((value) => value + 1), []);

  return { data, loading, error, ready, reload };
}

/** 一组模块是否全部处于首次加载中——用于决定整页骨架屏还是分块渲染。 */
export function allPending(modules: AsyncModule<unknown>[]): boolean {
  return modules.length > 0 && modules.every((item) => item.loading && !item.ready);
}
