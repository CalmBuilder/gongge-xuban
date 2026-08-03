import { cn } from '@/lib/utils';
import logoMark from '../assets/brand/gongge-mark.svg';

export type BrandContext = 'login' | 'management' | 'workspace';

export type BrandLogoProps = {
  /** Product surface controls the displayed member of the brand family. */
  context?: BrandContext;
  /** Hide the wordmark and only render the logo mark. */
  markOnly?: boolean;
  /** Size of the square logo mark in pixels. */
  markSize?: number;
  className?: string;
  /** Extra classes applied to the wordmark wrapper (e.g. to hide it responsively). */
  wordmarkClassName?: string;
};

const WORDMARKS: Record<BrandContext, string> = {
  login: '共格·序伴',
  management: '共格',
  workspace: '序伴',
};

/** Unified brand lockup for the login, management and conversation surfaces. */
export default function BrandLogo({
  context = 'management',
  markOnly = false,
  markSize = 28,
  className,
  wordmarkClassName,
}: BrandLogoProps) {
  const wordmark = WORDMARKS[context];

  return (
    <span className={cn('flex items-center gap-[8px] overflow-hidden p-[4px]', className)}>
      <img
        src={logoMark}
        alt={wordmark}
        className="shrink-0"
        style={{ width: markSize, height: markSize }}
      />
      {!markOnly && (
        <span className={cn('flex flex-col gap-[2px] leading-none', wordmarkClassName)}>
          <strong className="whitespace-nowrap text-[17px] font-semibold leading-none tracking-[0.02em] text-[#0b1f47]">
            {wordmark}
          </strong>
        </span>
      )}
    </span>
  );
}
