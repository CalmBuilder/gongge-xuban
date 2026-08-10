import type { ToolReliabilityContract } from '@/types';

/** Parse the advanced reliability editor without weakening server-side contract validation. */
export function parseToolReliabilityContract(
  value: string,
): ToolReliabilityContract | null {
  const normalized = value.trim();
  if (!normalized) return null;
  const parsed: unknown = JSON.parse(normalized);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('工具可靠性契约必须是 JSON object');
  }
  return parsed as ToolReliabilityContract;
}
