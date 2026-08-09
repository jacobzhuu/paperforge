'use client';

import * as React from 'react';
import { useReportWebVitals } from 'next/web-vitals';
import type { NextWebVitalsMetric } from 'next/app';

/**
 * 本轮只建立浏览器侧测量点，不向生产环境发送用户数据。
 * 开发环境输出可直接用于 Performance 面板；自定义事件便于自动化测试采集。
 */
export function WebVitalsReporter() {
  const report = React.useCallback((metric: NextWebVitalsMetric) => {
    const enriched = metric as NextWebVitalsMetric & {
      rating?: string;
      navigationType?: string;
    };
    if (process.env.NODE_ENV !== 'production') {
      console.info(
        `[web-vitals] ${metric.name}=${Math.round(metric.value)} (${enriched.rating ?? 'n/a'})`,
      );
    }
    window.dispatchEvent(
      new CustomEvent('paperforge:web-vital', {
        detail: {
          id: metric.id,
          name: metric.name,
          value: metric.value,
          rating: enriched.rating,
          navigationType: enriched.navigationType,
        },
      }),
    );
  }, []);

  useReportWebVitals(report);
  return null;
}
