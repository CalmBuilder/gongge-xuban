import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChevronRight, LoaderCircle, Search, X } from 'lucide-react';

import { api } from '@/api/client';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type {
  OrganizationSearchResult,
  OrganizationUnit,
  OrganizationUnitNode,
} from '@/types/organization';

type OrganizationTreeNavigatorProps = {
  tenantId: string;
  selectedId: string;
  onSelect: (unit: OrganizationUnit) => void;
  refreshToken?: number;
  selectRootOnInitialize?: boolean;
  className?: string;
};

export function OrganizationTreeNavigator({
  tenantId,
  selectedId,
  onSelect,
  refreshToken = 0,
  selectRootOnInitialize = true,
  className,
}: OrganizationTreeNavigatorProps) {
  const [nodes, setNodes] = useState<Record<string, OrganizationUnitNode>>({});
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [loadedParents, setLoadedParents] = useState<Set<string>>(() => new Set());
  const [loadingParents, setLoadingParents] = useState<Set<string>>(() => new Set());
  const [searchText, setSearchText] = useState('');
  const [searchResults, setSearchResults] = useState<OrganizationSearchResult[]>([]);
  const [searching, setSearching] = useState(false);
  const [treeError, setTreeError] = useState('');
  const onSelectRef = useRef(onSelect);

  useEffect(() => {
    onSelectRef.current = onSelect;
  }, [onSelect]);

  const mergeNodes = useCallback((rows: OrganizationUnitNode[]) => {
    setNodes((current) => {
      const next = { ...current };
      rows.forEach((row) => {
        next[row.id] = row;
      });
      return next;
    });
  }, []);

  const loadChildren = useCallback(async (parentId: string) => {
    setLoadingParents((current) => new Set(current).add(parentId));
    try {
      const rows = await api.get<OrganizationUnitNode[]>(
        `/api/organization/unit-children?tenant_id=${encodeURIComponent(tenantId)}`
        + `&parent_id=${encodeURIComponent(parentId)}`,
      );
      mergeNodes(rows);
      setLoadedParents((current) => new Set(current).add(parentId));
      setTreeError('');
      return rows;
    } catch {
      setTreeError('该层组织暂时无法加载，请稍后重试。');
      return [];
    } finally {
      setLoadingParents((current) => {
        const next = new Set(current);
        next.delete(parentId);
        return next;
      });
    }
  }, [mergeNodes, tenantId]);

  useEffect(() => {
    let cancelled = false;
    async function initializeTree() {
      try {
        const roots = await api.get<OrganizationUnitNode[]>(
          `/api/organization/unit-children?tenant_id=${encodeURIComponent(tenantId)}`,
        );
        if (cancelled) return;
        setNodes(Object.fromEntries(roots.map((row) => [row.id, row])));
        setExpanded(new Set(roots.map((row) => row.id)));
        setLoadedParents(new Set());
        setTreeError('');
        const root = roots[0];
        if (root) {
          if (selectRootOnInitialize) onSelectRef.current(root);
          if (root.has_children) await loadChildren(root.id);
        }
      } catch {
        if (!cancelled) setTreeError('组织树暂时无法加载，成员列表仍可继续使用。');
      }
    }
    void initializeTree();
    return () => {
      cancelled = true;
    };
  }, [loadChildren, refreshToken, selectRootOnInitialize, tenantId]);

  useEffect(() => {
    const keyword = searchText.trim();
    if (!keyword) {
      setSearchResults([]);
      return;
    }
    const timeout = window.setTimeout(async () => {
      setSearching(true);
      try {
        setSearchResults(await api.get<OrganizationSearchResult[]>(
          `/api/organization/unit-search?tenant_id=${encodeURIComponent(tenantId)}`
          + `&keyword=${encodeURIComponent(keyword)}&limit=20`,
        ));
      } catch {
        setSearchResults([]);
        setTreeError('组织搜索暂时不可用。');
      } finally {
        setSearching(false);
      }
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [searchText, tenantId]);

  const visibleNodes = useMemo(() => {
    const childrenByParent = new Map<string | null, OrganizationUnitNode[]>();
    const nodeIds = new Set(Object.keys(nodes));
    Object.values(nodes).forEach((node) => {
      const visibleParentId = node.parent_id && nodeIds.has(node.parent_id)
        ? node.parent_id
        : null;
      const children = childrenByParent.get(visibleParentId) || [];
      children.push(node);
      childrenByParent.set(visibleParentId, children);
    });
    childrenByParent.forEach((children) => {
      children.sort((left, right) => (
        left.sort_order - right.sort_order
        || left.name.localeCompare(right.name, 'zh-CN')
        || left.id.localeCompare(right.id)
      ));
    });
    const result: OrganizationUnitNode[] = [];
    const visit = (parentId: string | null) => {
      (childrenByParent.get(parentId) || []).forEach((node) => {
        result.push(node);
        if (expanded.has(node.id)) visit(node.id);
      });
    };
    visit(null);
    return result;
  }, [expanded, nodes]);

  async function toggleNode(node: OrganizationUnitNode) {
    if (!node.has_children) return;
    if (expanded.has(node.id)) {
      setExpanded((current) => {
        const next = new Set(current);
        next.delete(node.id);
        return next;
      });
      return;
    }
    setExpanded((current) => new Set(current).add(node.id));
    if (!loadedParents.has(node.id)) await loadChildren(node.id);
  }

  async function selectSearchResult(result: OrganizationSearchResult) {
    const ancestorIds = result.path.slice(0, -1).map((item) => item.id);
    const missingParents = ancestorIds.filter((id) => !loadedParents.has(id));
    const childGroups = await Promise.all(missingParents.map((id) => loadChildren(id)));
    mergeNodes([...childGroups.flat(), result]);
    setExpanded((current) => new Set([...current, ...ancestorIds]));
    onSelect(result);
    setSearchText('');
    setSearchResults([]);
  }

  return (
    <div className={cn('min-w-0', className)}>
      <label className="relative block">
        <Search className="pointer-events-none absolute left-[10px] top-1/2 size-[14px] -translate-y-1/2 text-[#858b9c]" />
        <input
          aria-label="搜索组织"
          className="gg-type-control h-[34px] w-full rounded-[10px] border border-[#e3e7f1] bg-white pl-[31px] pr-[30px] text-[var(--gg-text-primary)] placeholder:text-[var(--gg-text-muted)] outline-none focus:border-[#3157e8]"
          onChange={(event) => setSearchText(event.target.value)}
          placeholder="按名称或编码定位"
          value={searchText}
        />
        {searchText ? (
          <button
            aria-label="清除组织搜索"
            className="absolute right-[8px] top-1/2 grid size-[18px] -translate-y-1/2 place-items-center text-[#9aa3b7]"
            onClick={() => setSearchText('')}
            type="button"
          >
            <X className="size-[13px]" />
          </button>
        ) : null}
      </label>

      {searchText ? (
        <div className="mt-[8px] max-h-[390px] overflow-y-auto rounded-[10px] border border-[#e5e9f2] bg-white p-[4px]">
          {searching ? (
            <p className="gg-type-meta flex items-center gap-[6px] px-[9px] py-[12px]">
              <LoaderCircle className="size-[14px] animate-spin" />正在定位组织…
            </p>
          ) : searchResults.length ? searchResults.map((result) => (
            <button
              className="block w-full rounded-[8px] px-[9px] py-[8px] text-left hover:bg-[#f3f6fc]"
              key={result.id}
              onClick={() => void selectSearchResult(result)}
              type="button"
            >
              <strong className="gg-type-control block text-[#303748]">{result.name}</strong>
              <span className="gg-type-caption mt-[2px] block truncate">
                {result.path.map((item) => item.name).join(' / ')}
              </span>
            </button>
          )) : (
            <p className="gg-type-meta px-[9px] py-[12px]">没有匹配的组织</p>
          )}
        </div>
      ) : (
        <div aria-label="企业组织树" className="mt-[8px] grid gap-[2px]" role="tree">
          {visibleNodes.map((node) => (
            <div
              className={cn(
                'flex min-h-[36px] items-center rounded-[9px] pr-[6px]',
                selectedId === node.id ? 'bg-[#eaf0ff] text-[#3157e8]' : 'text-[#596174] hover:bg-[#f0f3f9]',
                node.status === 'inactive' && 'opacity-55',
              )}
              key={node.id}
              role="treeitem"
              aria-selected={selectedId === node.id}
              onClick={() => onSelect(node)}
              onKeyDown={(event) => {
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault();
                  onSelect(node);
                }
              }}
              style={{ paddingLeft: `${5 + node.depth * 15}px` }}
              tabIndex={0}
            >
              <Button
                aria-label={expanded.has(node.id) ? `收起${node.name}` : `展开${node.name}`}
                className="size-[26px] shrink-0 p-0"
                disabled={!node.has_children || loadingParents.has(node.id)}
                onClick={(event) => {
                  event.stopPropagation();
                  void toggleNode(node);
                }}
                size="icon"
                type="button"
                variant="ghost"
              >
                {loadingParents.has(node.id) ? (
                  <LoaderCircle className="size-[13px] animate-spin" />
                ) : (
                  <ChevronRight className={cn('size-[13px] transition-transform', expanded.has(node.id) && 'rotate-90')} />
                )}
              </Button>
              <button
                className="gg-type-control min-w-0 flex-1 truncate py-[8px] text-left"
                onClick={(event) => {
                  event.stopPropagation();
                  onSelect(node);
                }}
                type="button"
              >
                {node.name}
              </button>
            </div>
          ))}
        </div>
      )}
      {treeError ? (
        <p className="gg-type-control mt-[8px] rounded-[8px] bg-[#fff4f2] px-[9px] py-[7px] text-[#b94a3d]">
          {treeError}
        </p>
      ) : null}
    </div>
  );
}
