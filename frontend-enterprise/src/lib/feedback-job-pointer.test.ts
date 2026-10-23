import { beforeEach, expect, it } from 'vitest';
import { readFeedbackJobPointer, removeFeedbackJobPointer, writeFeedbackJobPointer } from './feedback-job-pointer';

beforeEach(() => window.sessionStorage.clear());

it('只保存任务定位，非法缓存不被恢复成正文或授权', () => {
  writeFeedbackJobPointer('pointer-a', 'job-a');
  expect(JSON.parse(window.sessionStorage.getItem('pointer-a') || '')).toEqual({ job_id: 'job-a' });
  expect(readFeedbackJobPointer('pointer-a')).toBe('job-a');
  window.sessionStorage.setItem('invalid', JSON.stringify({ draft_skill: { secret: true } }));
  expect(readFeedbackJobPointer('invalid')).toBeNull();
  window.sessionStorage.setItem('malformed', '{');
  expect(readFeedbackJobPointer('malformed')).toBeNull();
  removeFeedbackJobPointer('pointer-a');
  expect(readFeedbackJobPointer('pointer-a')).toBeNull();
});
