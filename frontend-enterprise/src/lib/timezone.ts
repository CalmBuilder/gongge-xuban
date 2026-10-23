import { getDateLocale } from '@/i18n';

const FALLBACK_TIME_ZONE = 'Asia/Shanghai';

export function getClientTimeZone(): string {
  return getDetectedClientTimeZone() || FALLBACK_TIME_ZONE;
}

export function getDetectedClientTimeZone(): string | undefined {
  try {
    const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone?.trim();
    if (!timeZone) return undefined;
    new Intl.DateTimeFormat('en-US', { timeZone }).format();
    return timeZone;
  } catch {
    return undefined;
  }
}

export function parseBackendDateTime(value?: string): Date {
  const text = String(value || '').trim();
  if (!text) return new Date('');
  if (/[zZ]|[+-]\d{2}:\d{2}$/.test(text)) return new Date(text);
  return new Date(`${text}Z`);
}

export function formatClientDateTime(value?: string, emptyText = '-'): string {
  if (!value) return emptyText;
  const date = parseBackendDateTime(value);
  if (Number.isNaN(date.getTime())) return emptyText;
  return date.toLocaleString(getDateLocale(), {
    hour12: false,
    timeZone: getClientTimeZone(),
  });
}
