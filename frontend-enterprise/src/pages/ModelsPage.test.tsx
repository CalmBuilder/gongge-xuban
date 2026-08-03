import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import { modelActionError } from './ModelsPage';

describe('modelActionError', () => {
  it('将默认模型并发冲突转换为可操作提示', () => {
    const error = new ApiError(
      409,
      JSON.stringify({ detail: 'MODEL_DEFAULT_CONFLICT' }),
      'Conflict',
    );

    expect(modelActionError(error, '保存失败')).toBe('默认模型状态已变化，请刷新后重试');
  });

  it('保留普通接口错误信息和无信息时的回退文案', () => {
    expect(modelActionError(new Error('模型名称无效'), '保存失败')).toBe('模型名称无效');
    expect(modelActionError(null, '保存失败')).toBe('保存失败');
  });
});
