/**
 * @Time       : 2026/09/27
 * @Author     : zhanglp8181
 * @File       : sd3-date-evidence.ts
 * @CallChain  : SD-3 浏览器验收 → 产品回复正文 → 明确日期证据提取
 * @Description: 从产品输出提取独立日历日期，不读取测试预期或客户端换算结果。
 */

const DATE_SECTION_PATTERN = /目标日期（本轮新输入）[\s\S]{0,480}/;
const ISO_DATE_PATTERN = /\b(20\d{2})-(\d{1,2})-(\d{1,2})\b/g;
const CHINESE_DATE_PATTERN = /(20\d{2})年(\d{1,2})月(\d{1,2})日/g;

function normalizeDateParts(year: string, month: string, day: string): string | null {
  /** 校验提取的年月日确实组成一个日历日期，并统一为 ISO 表示。 */

  const yearNumber = Number(year);
  const monthNumber = Number(month);
  const dayNumber = Number(day);
  const date = new Date(Date.UTC(yearNumber, monthNumber - 1, dayNumber));
  if (
    date.getUTCFullYear() !== yearNumber
    || date.getUTCMonth() !== monthNumber - 1
    || date.getUTCDate() !== dayNumber
  ) {
    return null;
  }
  return [yearNumber, monthNumber, dayNumber]
    .map((value, index) => (index === 0 ? String(value) : String(value).padStart(2, '0')))
    .join('-');
}

export function extractExplicitCalendarDate(content: string): string | null {
  /** 只从“目标日期（本轮新输入）”章节返回产品明确写出的日历日期。 */

  const section = String(content || '').match(DATE_SECTION_PATTERN)?.[0] || '';
  if (!section) return null;

  const candidates: Array<{ index: number; value: string }> = [];
  for (const match of section.matchAll(ISO_DATE_PATTERN)) {
    const value = normalizeDateParts(match[1], match[2], match[3]);
    if (value && match.index !== undefined) candidates.push({ index: match.index, value });
  }
  for (const match of section.matchAll(CHINESE_DATE_PATTERN)) {
    const value = normalizeDateParts(match[1], match[2], match[3]);
    if (value && match.index !== undefined) candidates.push({ index: match.index, value });
  }
  candidates.sort((left, right) => left.index - right.index);
  return candidates.length > 0 ? candidates[0].value : null;
}
