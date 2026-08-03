import { Link } from 'react-router-dom';

import PlazaResourceIcon from '@/components/openPlatform/PlazaResourceIcon';
import { cn } from '@/lib/utils';
import { ApiOutlined, CheckOutlined } from '@/icons';

type ToolType = 'http' | 'mcp';

type ToolTypeOption = {
  value: ToolType;
  label: string;
  hint: string;
  to: string;
};

const TOOL_TYPE_OPTIONS: ToolTypeOption[] = [
  { value: 'http', label: 'HTTP 工具', hint: '配置单个 HTTP 接口作为工具', to: '/enterprise/tools/new' },
  { value: 'mcp', label: 'MCP 服务器', hint: '连接 MCP Server，自动发现并同步其工具集', to: '/enterprise/tools/mcp/new' },
];

/**
 * 在新建流程中切换工具接入方式，并以项目钴蓝令牌标识当前选择。
 */
export default function ToolTypeSwitcher({ active }: { active: ToolType }) {
  return (
    <div className="mb-[16px] min-w-0">
      <p className="mb-[8px] text-[13px] font-medium text-[var(--gg-ink)]">工具类型</p>
      <nav className="grid grid-cols-1 gap-[10px] sm:grid-cols-2" aria-label="工具类型">
        {TOOL_TYPE_OPTIONS.map((option) => {
          const isActive = option.value === active;

          return (
            <Link
              key={option.value}
              to={option.to}
              className={cn(
                'group relative flex min-h-[72px] min-w-0 items-center gap-[12px] overflow-hidden rounded-[var(--gg-radius-card)] border px-[16px] py-[12px] text-left transition-[background-color,border-color,box-shadow,transform]',
                isActive
                  ? 'border-[var(--gg-cobalt)] bg-[color-mix(in_srgb,var(--gg-cobalt)_6%,var(--gg-paper))] shadow-[0_8px_22px_rgba(49,87,232,0.10)]'
                  : 'border-[var(--gg-border)] bg-[var(--gg-paper)] hover:border-[#bfcbea] hover:bg-[var(--gg-cloud)]',
              )}
              aria-current={isActive ? 'page' : undefined}
              aria-label={option.label}
            >
              <span
                aria-hidden="true"
                className={cn(
                  'absolute inset-y-[10px] left-0 w-[3px] rounded-r-full transition-colors',
                  isActive ? 'bg-[var(--gg-cobalt)]' : 'bg-transparent',
                )}
              />
              <span
                className={cn(
                  'flex size-[36px] shrink-0 items-center justify-center rounded-[11px] transition-colors',
                  isActive
                    ? 'bg-[color-mix(in_srgb,var(--gg-cobalt)_12%,var(--gg-paper))] text-[var(--gg-cobalt)]'
                    : 'bg-[var(--gg-cloud)] text-[var(--gg-slate)] group-hover:text-[var(--gg-cobalt)]',
                )}
              >
                {option.value === 'mcp' ? (
                  <ApiOutlined className="size-[17px] shrink-0" />
                ) : (
                  <PlazaResourceIcon kind="tools" size="micro" />
                )}
              </span>
              <span className="flex min-w-0 flex-1 flex-col gap-[3px] pr-[24px]">
                <span className="text-[13px] font-semibold text-[var(--gg-ink)]">{option.label}</span>
                <span className="text-[12px] leading-[1.5] text-[var(--gg-slate)]">{option.hint}</span>
              </span>
              {isActive ? (
                <span
                  className="absolute right-[14px] top-[14px] flex size-[18px] items-center justify-center rounded-full bg-[var(--gg-cobalt)] text-white"
                  aria-hidden="true"
                >
                  <CheckOutlined className="size-[10px] shrink-0" />
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
