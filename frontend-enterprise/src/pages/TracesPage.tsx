import { ReloadOutlined } from '../icons';
import { useEffect, useState } from 'react';
import {
  Button as UIButton,
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  notify,
} from '@/components/ui';
import { DataTable, type DataTableColumn } from '@/components/DataTable';
import { Paginator } from '@/components/Paginator';
import { useClientPagination } from '../hooks/useClientPagination';
import { api, getRequestTenantId } from '../api/client';
import type { TraceSummary } from '../types';

const TRACES_PAGE_SIZE = 10;

export default function TracesPage() {
  const [rows, setRows] = useState<TraceSummary[]>([]);
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);

  const load = () =>
    api
      .get<TraceSummary[]>(`/api/enterprise/traces?tenant_id=${getRequestTenantId()}`)
      .then(setRows)
      .catch((error) => notify.error(error.message));

  useEffect(() => {
    void load();
  }, []);

  const pagination = useClientPagination(rows, TRACES_PAGE_SIZE);

  async function openDetail(row: TraceSummary) {
    const result = await api.get<Record<string, unknown>>(`/api/enterprise/traces/${row.session_id}?tenant_id=${getRequestTenantId()}`);
    setDetail(result);
  }

  const truncateCell = 'block truncate';
  const columns: DataTableColumn<TraceSummary>[] = [
    { key: 'session_id', title: '会话 ID', width: 230, render: (row) => <span className={truncateCell}>{row.session_id}</span> },
    { key: 'user_id', title: '用户 ID', width: 150, render: (row) => <span className={truncateCell}>{row.user_id || '-'}</span> },
    { key: 'active_skill_id', title: '当前技能', width: 190, render: (row) => <span className={truncateCell}>{row.active_skill_id || '-'}</span> },
    { key: 'active_step_id', title: '当前 Step', width: 190, render: (row) => <span className={truncateCell}>{row.active_step_id || '-'}</span> },
    { key: 'tool_call_count', title: '工具调用', width: 96, render: (row) => row.tool_call_count },
    { key: 'status', title: '状态', width: 96, render: (row) => row.status },
    { key: 'updated_at', title: '更新时间', width: 210, render: (row) => <span className={truncateCell}>{row.updated_at}</span> },
    {
      key: 'actions',
      title: '操作',
      width: 96,
      render: (row) => (
        <UIButton
          variant="outline"
          size="sm"
          className="h-[28px] rounded-[8px] px-[12px] gg-type-meta"
          onClick={() => void openDetail(row)}
        >
          查看
        </UIButton>
      ),
    },
  ];

  return (
    <>
      <div className="page-title">
        <h3 className="gg-type-page-title">Trace</h3>
        <UIButton variant="outline" onClick={() => void load()}>
          <ReloadOutlined />
          刷新
        </UIButton>
      </div>
      <Card className="data-card">
        <CardHeader>
          <CardTitle>会话 Trace</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-[16px]">
          <div className="overflow-x-auto">
            <DataTable
              aria-label="会话 Trace"
              columns={columns}
              data={pagination.pagedItems}
              rowKey={(row) => row.session_id}
              className="min-w-[1308px]"
              emptyText="暂无 Trace"
            />
          </div>
          {rows.length > 0 && (
            <Paginator
              aria-label="Trace 分页"
              className="mt-0"
              page={pagination.page}
              pageCount={pagination.pageCount}
              onChange={pagination.setPage}
            />
          )}
        </CardContent>
      </Card>
      <Sheet open={Boolean(detail)} onOpenChange={(next) => { if (!next) setDetail(null); }}>
        <SheetContent side="right" className="w-[720px] sm:max-w-[720px]">
          <SheetHeader>
            <SheetTitle>Trace Detail</SheetTitle>
          </SheetHeader>
          <div className="min-h-0 flex-1 overflow-auto px-[16px] pb-[16px]">
            <pre className="gg-type-meta whitespace-pre-wrap">{JSON.stringify(detail, null, 2)}</pre>
          </div>
        </SheetContent>
      </Sheet>
    </>
  );
}
