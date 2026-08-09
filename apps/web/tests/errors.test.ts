import { describe, expect, it } from 'vitest';

import { describeError } from '@/lib/errors';

describe('describeError', () => {
  it('preserves a cross-realm or proxy error message', () => {
    expect(describeError({ message: '上游连接已断开', code: 'ECONNRESET' })).toBe(
      '上游连接已断开',
    );
  });

  it('uses structured proxy details instead of hiding them as an unknown error', () => {
    expect(describeError({ status: 502, detail: 'socket hang up' })).toBe('socket hang up');
  });
});
