/** 刷新只恢复不含正文的任务定位；有效性始终由服务器job/source/fence确认。 */
export function readFeedbackJobPointer(key: string): string | null {
  try {
    const value: unknown = JSON.parse(window.sessionStorage.getItem(key) || 'null');
    if (!value || typeof value !== 'object' || !('job_id' in value)
      || typeof value.job_id !== 'string' || !value.job_id || value.job_id.length > 512) return null;
    return value.job_id;
  } catch { return null; }
}
export function writeFeedbackJobPointer(key: string, jobId: string) {
  window.sessionStorage.setItem(key, JSON.stringify({ job_id: jobId }));
}
export function removeFeedbackJobPointer(key: string) { window.sessionStorage.removeItem(key); }
