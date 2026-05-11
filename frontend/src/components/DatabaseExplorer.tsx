import { useEffect, useMemo, useState } from 'react';
import { api } from '@/lib/api';
import type { DbTableInfo, DbTableRows } from '@/types';

// Inline value renderer — JSON / dates / numbers / nulls
function renderCell(v: unknown): string {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  if (typeof v === 'string') {
    // dates already as ISO strings
    return v.length > 80 ? v.slice(0, 77) + '…' : v;
  }
  if (typeof v === 'number') {
    if (!Number.isFinite(v)) return String(v);
    if (Math.abs(v) > 1000) return v.toLocaleString('en-IN');
    return String(v);
  }
  if (typeof v === 'boolean') return v ? '✓' : '✗';
  return String(v);
}

function TableList({
  tables,
  selected,
  onSelect,
}: { tables: DbTableInfo[]; selected: string | null; onSelect: (t: string) => void }) {
  return (
    <div className="flex flex-col h-full border-r border-gray-800 overflow-y-auto">
      <div className="flex-shrink-0 px-2 py-2 border-b border-gray-800">
        <div className="text-yellow-400 text-xs uppercase tracking-wider font-bold">
          Tables ({tables.length})
        </div>
        <div className="text-[10px] text-gray-600">read-only</div>
      </div>
      {tables.map((t) => {
        const sel = selected === t.table;
        return (
          <button
            key={t.table}
            onClick={() => onSelect(t.table)}
            className={`text-left px-2 py-1.5 border-b border-gray-900 hover:bg-gray-900 transition-colors ${
              sel ? 'bg-yellow-500/10 border-l-2 border-l-yellow-500' : ''
            }`}
          >
            <div className="text-xs text-gray-200 font-mono">{t.table}</div>
            <div className="text-[10px] text-gray-500">
              {t.rows === null ? '—' : t.rows.toLocaleString('en-IN')} rows
              {' · '}
              {t.columns.length} cols
            </div>
          </button>
        );
      })}
    </div>
  );
}

function TableView({ table }: { table: string }) {
  const [data, setData] = useState<DbTableRows | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState('');
  const [page, setPage] = useState(0);
  const PAGE = 50;

  useEffect(() => {
    setLoading(true);
    setError(null);
    api
      .getDbTable(table, { q: q || undefined, limit: PAGE, offset: page * PAGE })
      .then((d) => setData(d as DbTableRows))
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [table, q, page]);

  // Reset paging when search or table changes
  useEffect(() => { setPage(0); }, [q, table]);

  const cols = data?.columns ?? [];
  const totalPages = data ? Math.ceil(data.total / PAGE) : 0;

  return (
    <div className="flex flex-col h-full overflow-hidden bg-gray-950">
      {/* Header */}
      <div className="flex-shrink-0 flex items-center gap-3 px-3 py-2 border-b border-gray-800 bg-gray-900/40">
        <span className="text-yellow-400 text-xs font-mono font-bold">{table}</span>
        <input
          type="text"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search…"
          className="bg-gray-900 text-gray-100 px-2 py-1 text-xs rounded border border-gray-700 focus:outline-none focus:border-yellow-600 w-48"
        />
        <span className="text-[10px] text-gray-500 ml-auto">
          {loading ? 'loading…' : data ? `${data.total.toLocaleString('en-IN')} rows · page ${page + 1}/${totalPages || 1}` : ''}
        </span>
        <button
          onClick={() => setPage((p) => Math.max(0, p - 1))}
          disabled={page === 0 || loading}
          className="px-2 py-0.5 text-[10px] rounded text-gray-400 hover:text-gray-200 disabled:opacity-30"
        >
          ‹ prev
        </button>
        <button
          onClick={() => setPage((p) => p + 1)}
          disabled={!data || page >= totalPages - 1 || loading}
          className="px-2 py-0.5 text-[10px] rounded text-gray-400 hover:text-gray-200 disabled:opacity-30"
        >
          next ›
        </button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto">
        {error ? (
          <div className="p-4 text-xs text-red-400">{error}</div>
        ) : !data ? null : data.rows.length === 0 ? (
          <div className="p-4 text-xs text-gray-500">No rows match.</div>
        ) : (
          <table className="text-[11px] font-mono">
            <thead className="sticky top-0 bg-gray-950 border-b border-gray-800 z-10">
              <tr className="text-gray-500 text-left">
                {cols.map((c) => (
                  <th key={c} className="px-2 py-1 font-normal whitespace-nowrap">{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.rows.map((r, i) => (
                <tr key={i} className="border-b border-gray-900 hover:bg-gray-900/40 align-top">
                  {cols.map((c) => {
                    const v = r[c];
                    const isJson = v !== null && typeof v === 'object';
                    return (
                      <td
                        key={c}
                        className={`px-2 py-1 whitespace-nowrap text-gray-300 ${isJson ? 'text-amber-300' : ''}`}
                        title={isJson ? JSON.stringify(v, null, 2) : String(v ?? '')}
                      >
                        {renderCell(v)}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export function DatabaseExplorer() {
  const [tables, setTables] = useState<DbTableInfo[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDbTables()
      .then((d) => {
        const list = (d.tables as DbTableInfo[]).sort((a, b) => (b.rows ?? 0) - (a.rows ?? 0));
        setTables(list);
        if (list.length > 0 && !selected) setSelected(list[0].table);
      })
      .catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const memoTables = useMemo(() => tables, [tables]);

  return (
    <div className="h-full grid grid-cols-12 gap-0">
      <div className="col-span-2 min-h-0">
        {err ? (
          <div className="p-3 text-xs text-red-400">{err}</div>
        ) : (
          <TableList tables={memoTables} selected={selected} onSelect={setSelected} />
        )}
      </div>
      <div className="col-span-10 min-h-0">
        {selected ? (
          <TableView table={selected} />
        ) : (
          <div className="flex h-full items-center justify-center text-xs text-gray-500">
            Select a table to browse
          </div>
        )}
      </div>
    </div>
  );
}
