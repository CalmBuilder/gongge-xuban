import { PLAZA_RESOURCE_ARTWORK } from '@/assets/plaza/plaza-artwork';
import type { PlazaResourceKind } from '@/assets/plaza/plaza-resource-icons';
import { cn } from '@/lib/utils';

type PlazaResourceArtworkSize = 'micro' | 'compact' | 'card' | 'drawer';

export type PlazaResourceArtworkProps = {
  kind: PlazaResourceKind;
  size?: PlazaResourceArtworkSize;
  className?: string;
};

// The source PNGs carry generous transparent padding, so each step renders a
// bit larger than the flat PlazaResourceIcon tile it replaces to keep the
// visible glyph at a comparable optical size.
const SIZE_CLASS = {
  micro: 'size-[32px]',
  compact: 'size-[36px]',
  card: 'size-[80px]',
  drawer: 'size-[54px]',
} satisfies Record<PlazaResourceArtworkSize, string>;

/**
 * Dimensional 3D-glass category artwork used on the 开放广场 surfaces (column
 * headers, resource cards, drawers). It replaces the flat lucide tile so the
 * resource modules visually match the 数字员工 avatar cards.
 */
export default function PlazaResourceArtwork({
  kind,
  size = 'card',
  className,
}: PlazaResourceArtworkProps) {
  return (
    <img
      src={PLAZA_RESOURCE_ARTWORK[kind]}
      alt=""
      aria-hidden="true"
      draggable={false}
      data-plaza-resource-artwork={kind}
      className={cn(
        'shrink-0 select-none object-contain drop-shadow-[0_2px_6px_rgba(15,23,42,0.14)]',
        SIZE_CLASS[size],
        className,
      )}
    />
  );
}
