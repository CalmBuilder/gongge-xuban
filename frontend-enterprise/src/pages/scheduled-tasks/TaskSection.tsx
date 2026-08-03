import type { ReactNode } from 'react';

import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { UnderlineTabs, type UnderlineTabItem } from '@/components/ui';

export type TaskSectionProps<TFilter extends string, TRow> = {
  /** Leading header icon (14px). */
  icon: ReactNode;
  title: string;
  filterTabs: UnderlineTabItem<TFilter>[];
  filter: TFilter;
  onFilterChange: (value: TFilter) => void;
  /** Current result rows — drives empty state and paginator visibility. */
  rows: TRow[];
  /** Rows for the current page — drives both responsive presentations. */
  pagedRows: TRow[];
  columns: DataTableColumn<TRow>[];
  rowKey: (row: TRow, index: number) => string | number;
  loading?: boolean;
  emptyText: string;
  tableSize?: 'default' | 'compact';
  striped?: boolean;
  bordered?: boolean;
  page: number;
  pageCount: number;
  onPageChange: (page: number) => void;
  /** Mobile (<768px) card renderer for a single row. */
  renderMobileCard: (row: TRow) => ReactNode;
};

/**
 * A titled list section used on the scheduled-tasks page: header (icon + title),
 * an underline filter tab bar, a responsive mobile card list, and a desktop
 * `DataTable` with a `Paginator`. Both the task list and the execution log use it.
 */
export function TaskSection<TFilter extends string, TRow>({
  icon,
  title,
  filterTabs,
  filter,
  onFilterChange,
  rows,
  pagedRows,
  columns,
  rowKey,
  loading,
  emptyText,
  tableSize = 'default',
  striped = false,
  bordered = false,
  page,
  pageCount,
  onPageChange,
  renderMobileCard,
}: TaskSectionProps<TFilter, TRow>) {
  return (
    <section aria-label={title}>
      <div className="mb-[16px] flex items-center gap-[6px] px-[12px] text-[#757f9c]">
        {icon}
        <span className="text-[14px] font-normal leading-none">{title}</span>
      </div>
      <UnderlineTabs
        aria-label={`${title}筛选`}
        variant="line"
        className="mb-[16px]"
        value={filter}
        onChange={onFilterChange}
        items={filterTabs}
      />
      <div className="grid gap-[10px] md:hidden">
        {pagedRows.length ? (
          pagedRows.map(renderMobileCard)
        ) : (
          <div className="py-[40px] text-center text-[13px] text-[#858b9c]">{emptyText}</div>
        )}
      </div>
      <div className="hidden md:block">
        <DataTable
          aria-label={title}
          columns={columns}
          data={pagedRows}
          rowKey={rowKey}
          loading={loading}
          emptyText={emptyText}
          size={tableSize}
          striped={striped}
          bordered={bordered}
        />
      </div>
      {rows.length > 0 && (
        <Paginator
          aria-label={`${title}分页`}
          page={page}
          pageCount={pageCount}
          onChange={onPageChange}
        />
      )}
    </section>
  );
}
