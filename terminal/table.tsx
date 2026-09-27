import { useMemo, useState, type ReactNode } from "react";

import { DataTableStackView, type DataTableCell, type DataTableColumn } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import { compareSortValues, type SortDirection } from "gloomberb/utils";

import type { Tone } from "./model";

/**
 * A column of a quantsys table: what it shows, what it sorts on, and its
 * colour. Keeping the three together is what stops a table from sorting on
 * one value while showing another.
 */
export interface QsColumn<T> extends DataTableColumn {
  text: (row: T) => string;
  sort?: (row: T) => string | number | null | undefined;
  tone?: (row: T) => Tone | undefined;
}

export function toneColor(tone: Tone | undefined): string | undefined {
  switch (tone) {
    case "positive": return colors.positive;
    case "negative": return colors.negative;
    case "warning": return colors.warning;
    case "muted": return colors.textDim;
    case "accent": return colors.textBright;
    default: return undefined;
  }
}

export function QsTable<T>({ items, columns, getKey, width, height, focused, detail, detailTitle,
  defaultSort, emptyTitle }: {
  items: readonly T[];
  columns: QsColumn<T>[];
  getKey: (row: T) => string;
  width: number;
  height: number;
  focused: boolean;
  detail?: (row: T) => ReactNode;
  detailTitle?: (row: T) => string;
  defaultSort?: { columnId: string; direction: SortDirection };
  emptyTitle: string;
}) {
  const [sort, setSort] = useState<{ columnId: string | null; direction: SortDirection }>(
    defaultSort ?? { columnId: null, direction: "desc" });
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const rows = useMemo(() => {
    const column = columns.find((c) => c.id === sort.columnId);
    if (!column?.sort) return [...items];
    const value = column.sort;
    return [...items].sort((a, b) => compareSortValues(value(a), value(b), sort.direction));
  }, [items, columns, sort]);

  const selected = rows.find((row) => getKey(row) === selectedKey) ?? rows[0] ?? null;

  const renderCell = (row: T, column: QsColumn<T>, rowState: { selected: boolean }): DataTableCell => ({
    text: column.text(row),
    color: rowState.selected ? colors.selectedText : toneColor(column.tone?.(row)) ?? colors.text,
  });

  return (
    <DataTableStackView<T, QsColumn<T>>
      focused={focused}
      detailOpen={detailOpen && !!selected && !!detail}
      onBack={() => setDetailOpen(false)}
      detailContent={selected && detail ? detail(selected) : null}
      detailTitle={selected && detailTitle ? detailTitle(selected) : undefined}
      selection={{
        kind: "id",
        selectedId: selected ? getKey(selected) : null,
        getId: getKey,
        onChange: (id) => setSelectedKey(id),
      }}
      onActivate={() => { if (detail) setDetailOpen(true); }}
      rootWidth={width}
      rootHeight={Math.max(1, height)}
      columns={columns}
      items={rows}
      getItemKey={getKey}
      sortColumnId={sort.columnId}
      sortDirection={sort.direction}
      onHeaderClick={(columnId) => {
        if (!columns.find((c) => c.id === columnId)?.sort) return;
        setSort((current) => current.columnId === columnId
          ? { columnId, direction: current.direction === "desc" ? "asc" : "desc" }
          : { columnId, direction: "desc" });
      }}
      onSortChange={(columnId, direction) => setSort({ columnId, direction })}
      renderCell={(row, column, _index, rowState) => renderCell(row, column, rowState)}
      emptyStateTitle={emptyTitle}
    />
  );
}
