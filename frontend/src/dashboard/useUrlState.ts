import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { parseFilters, writeFilters, type DashboardFilters } from './filters';

/**
 * The dashboard's shareable state in the query string: the open view
 * (?view=timeline) and the filters (see filters.ts). Writing never touches the
 * path or keeps a fragment, so an e2e #k= key that takeRoomKey already
 * stripped cannot come back into the address bar.
 */

export const VIEW_PARAM = 'view';

const VIEW_TOKEN = /^[a-z][a-z0-9-]{0,31}$/;

/** The ?view= value if it is well-formed; the caller decides if it is a real view. */
export function readViewParam(search: string): string | null {
  const raw = new URLSearchParams(search).get(VIEW_PARAM);
  return raw && VIEW_TOKEN.test(raw) ? raw : null;
}

export type FiltersUpdate = DashboardFilters | ((prev: DashboardFilters) => DashboardFilters);

export function useUrlState() {
  const [params, setParams] = useSearchParams();
  const search = params.toString();
  const filters = useMemo(() => parseFilters(new URLSearchParams(search)), [search]);
  const viewParam = useMemo(() => readViewParam(search), [search]);

  /** Filters edit in place (replace): typing must not flood the back button. */
  const setFilters = useCallback(
    (update: FiltersUpdate) => {
      setParams(
        (prev) =>
          writeFilters(prev, typeof update === 'function' ? update(parseFilters(prev)) : update),
        { replace: true },
      );
    },
    [setParams],
  );

  /** A view change is a history entry, so Back returns to the previous view. */
  const setViewParam = useCallback(
    (view: string | null) => {
      setParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (view) next.set(VIEW_PARAM, view);
          else next.delete(VIEW_PARAM);
          return next;
        },
        { replace: false },
      );
    },
    [setParams],
  );

  /**
   * Both at once. Two setSearchParams calls in one tick would each start
   * from the same old params and the second would drop the first's change.
   */
  const setViewAndFilters = useCallback(
    (view: string | null, update: FiltersUpdate) => {
      setParams(
        (prev) => {
          const next = writeFilters(
            prev,
            typeof update === 'function' ? update(parseFilters(prev)) : update,
          );
          if (view) next.set(VIEW_PARAM, view);
          else next.delete(VIEW_PARAM);
          return next;
        },
        { replace: false },
      );
    },
    [setParams],
  );

  return { filters, setFilters, viewParam, setViewParam, setViewAndFilters };
}

/** This page's link without any fragment: what "Copy link to this view" copies. */
export function currentViewLink(): string {
  const { origin, pathname, search } = window.location;
  return `${origin}${pathname}${search}`;
}
