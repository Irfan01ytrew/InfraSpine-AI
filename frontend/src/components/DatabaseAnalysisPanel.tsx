import { ChevronDown, Database } from "lucide-react";
import { cn } from "../lib/utils";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { ApiError, api, describeError } from "../api";
import type { DatabaseAnalysisCatalog, DatabaseAnalysisQuery, DatabaseAnalysisResult } from "../types";
import { Spinner } from "../icons";
import { SelectInput } from "./ui";

type Operation = DatabaseAnalysisQuery["operation"];
type FilterOperation = DatabaseAnalysisQuery["filters"][number]["operation"];

const OPERATIONS: Array<{ value: Operation; label: string }> = [
  { value: "rows", label: "Show rows" },
  { value: "count", label: "Count rows" },
  { value: "sum", label: "Sum" },
  { value: "mean", label: "Average" },
  { value: "min", label: "Minimum" },
  { value: "max", label: "Maximum" },
  { value: "count_distinct", label: "Count distinct values" },
];

const FILTER_OPERATIONS: Array<{ value: FilterOperation; label: string }> = [
  { value: "eq", label: "equals" },
  { value: "ne", label: "does not equal" },
  { value: "contains", label: "contains" },
  { value: "gt", label: "greater than" },
  { value: "gte", label: "at least" },
  { value: "lt", label: "less than" },
  { value: "lte", label: "at most" },
];

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) {
    return "The business database is not configured or could not safely serve this query.";
  }
  return describeError(error);
}

export function DatabaseAnalysisPanel() {
  const [open, setOpen] = useState(false);
  const [catalog, setCatalog] = useState<DatabaseAnalysisCatalog | null>(null);
  const [catalogRequested, setCatalogRequested] = useState(false);
  const [catalogBusy, setCatalogBusy] = useState(false);
  const [tableName, setTableName] = useState("");
  const [operation, setOperation] = useState<Operation>("rows");
  const [valueColumn, setValueColumn] = useState("");
  const [groupColumn, setGroupColumn] = useState("");
  const [filterColumn, setFilterColumn] = useState("");
  const [filterOperation, setFilterOperation] = useState<FilterOperation>("eq");
  const [filterValue, setFilterValue] = useState("");
  const [limit, setLimit] = useState(50);
  const [queryBusy, setQueryBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [result, setResult] = useState<DatabaseAnalysisResult | null>(null);
  const [resultQuery, setResultQuery] = useState<DatabaseAnalysisQuery | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || catalog || catalogRequested) return;
    setCatalogRequested(true);
    setCatalogBusy(true);
    api.databaseAnalysisCatalog()
      .then((loaded) => {
        setCatalog(loaded);
        setTableName(loaded.tables[0]?.name ?? "");
      })
      .catch((reason: unknown) => setError(errorMessage(reason)))
      .finally(() => setCatalogBusy(false));
  }, [catalog, catalogRequested, open]);

  const selectedTable = useMemo(
    () => catalog?.tables.find((table) => table.name === tableName) ?? null,
    [catalog, tableName],
  );
  const selectedFilterColumn = selectedTable?.columns.find((column) => column.name === filterColumn);
  const visibleFilterOperations = useMemo(() => FILTER_OPERATIONS.filter(({ value }) => {
    if (!selectedFilterColumn) return value === "eq" || value === "ne";
    if (selectedFilterColumn.type === "text") return value === "eq" || value === "ne" || value === "contains";
    if (selectedFilterColumn.type === "boolean") return value === "eq" || value === "ne";
    return value !== "contains";
  }), [selectedFilterColumn]);
  const valueColumns = selectedTable?.columns.filter((column) =>
    !["sum", "mean"].includes(operation) || column.type === "integer" || column.type === "numeric",
  ) ?? [];

  useEffect(() => {
    if (!visibleFilterOperations.some((item) => item.value === filterOperation)) {
      setFilterOperation(visibleFilterOperations[0]?.value ?? "eq");
    }
  }, [filterOperation, visibleFilterOperations]);

  async function runQuery(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedTable) return;
    setQueryBusy(true);
    setError("");
    setResult(null);
    try {
      const needsValue = !["rows", "count"].includes(operation);
      const query: DatabaseAnalysisQuery = {
        table: selectedTable.name,
        operation,
        ...(needsValue ? { value: valueColumn } : {}),
        group_by: operation === "rows" || !groupColumn ? [] : [groupColumn],
        filters: filterColumn && filterValue !== ""
          ? [{ column: filterColumn, operation: filterOperation, value: filterValue }]
          : [],
        limit,
      };
      const analysisResult = await api.runDatabaseAnalysis(query);
      setResult(analysisResult);
      setResultQuery(query);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setQueryBusy(false);
    }
  }

  async function exportResult() {
    if (!resultQuery || !result) return;
    setExportBusy(true);
    setError("");
    try {
      const blob = await api.exportDatabaseAnalysis(resultQuery);
      const href = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = href;
      link.download = `business-analysis-${result.table.replaceAll(".", "-")}.csv`;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(href), 1000);
    } catch (reason) {
      setError(errorMessage(reason));
    } finally {
      setExportBusy(false);
    }
  }

  return (
    <section className="border-b border-border" aria-label="Business database analysis">
      <button
        className="flex w-full cursor-pointer items-center gap-3 px-4 py-3 text-left outline-none hover:bg-surface-2 focus-visible:bg-surface-2"
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current);
          setError("");
        }}
      >
        <span className="grid size-8 shrink-0 place-items-center rounded-md bg-primary-soft text-primary"><Database className="size-4" /></span>
        <span className="min-w-0 flex-1">
          <span className="block text-[13px] font-medium text-foreground">Analyze business database</span>
          <span className="block text-[11px] text-muted">Read-only calculations on allowlisted tables</span>
        </span>
        <ChevronDown className={cn("size-4 shrink-0 text-muted transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="database-analysis-body px-4 pb-4">
          <div className="database-analysis-heading">
            <h4>Read-only business database</h4>
            <p>Choose an allowlisted table and a bounded calculation. SQL text is not accepted.</p>
          </div>
          {catalogBusy && <p className="database-analysis-status"><Spinner size={14} /> Loading configured tables…</p>}
          {error && !catalog && !catalogBusy && (
            <button className="btn btn-ghost btn-sm" type="button" onClick={() => {
              setError("");
              setCatalogRequested(false);
            }}>Retry loading database tables</button>
          )}
          {catalog && !catalog.enabled && (
            <p className="database-analysis-status" role="status">
              {catalog.message} An operator can configure a PostgreSQL URL or a rooted SQLite reporting file, with an allowlisted table/column catalog and department row grants.
            </p>
          )}
          {catalog?.enabled && selectedTable && (
            <form className="database-analysis-form" onSubmit={(event) => void runQuery(event)}>
              <label className="field">
                <span className="field-label">Source table</span>
                <SelectInput value={tableName} onChange={(event) => {
                  setTableName(event.target.value);
                  setValueColumn("");
                  setGroupColumn("");
                  setFilterColumn("");
                  setResult(null);
                }}>
                  {catalog.tables.map((table) => <option key={table.name} value={table.name}>{table.name}</option>)}
                </SelectInput>
              </label>
              <label className="field">
                <span className="field-label">Calculation</span>
                <SelectInput value={operation} onChange={(event) => setOperation(event.target.value as Operation)}>
                  {OPERATIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </SelectInput>
              </label>
              {!(["rows", "count"].includes(operation)) && (
                <label className="field">
                  <span className="field-label">Value column</span>
                  <SelectInput required value={valueColumn} onChange={(event) => setValueColumn(event.target.value)}>
                    <option value="">Choose a column</option>
                    {valueColumns.map((column) => <option key={column.name} value={column.name}>{column.name} · {column.type}</option>)}
                  </SelectInput>
                </label>
              )}
              {operation !== "rows" && (
                <label className="field">
                  <span className="field-label">Group by (optional)</span>
                  <SelectInput value={groupColumn} onChange={(event) => setGroupColumn(event.target.value)}>
                    <option value="">No grouping</option>
                    {selectedTable.columns.map((column) => <option key={column.name} value={column.name}>{column.name}</option>)}
                  </SelectInput>
                </label>
              )}
              <label className="field">
                <span className="field-label">Filter column (optional)</span>
                <SelectInput value={filterColumn} onChange={(event) => {
                  setFilterColumn(event.target.value);
                  setFilterValue("");
                }}>
                  <option value="">No filter</option>
                  {selectedTable.columns.map((column) => <option key={column.name} value={column.name}>{column.name} · {column.type}</option>)}
                </SelectInput>
              </label>
              {filterColumn && (
                <>
                  <label className="field">
                    <span className="field-label">Filter rule</span>
                    <SelectInput value={filterOperation} onChange={(event) => setFilterOperation(event.target.value as FilterOperation)}>
                      {visibleFilterOperations.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                    </SelectInput>
                  </label>
                  <label className="field">
                    <span className="field-label">Filter value</span>
                    {selectedFilterColumn?.type === "boolean" ? (
                      <SelectInput value={filterValue} required onChange={(event) => setFilterValue(event.target.value)}>
                        <option value="">Choose a value</option>
                        <option value="true">true</option>
                        <option value="false">false</option>
                      </SelectInput>
                    ) : (
                      <input
                        type={selectedFilterColumn?.type === "integer" || selectedFilterColumn?.type === "numeric" ? "number" : selectedFilterColumn?.type === "date" ? "date" : selectedFilterColumn?.type === "timestamp" ? "datetime-local" : "text"}
                        step={selectedFilterColumn?.type === "numeric" ? "any" : undefined}
                        value={filterValue}
                        maxLength={500}
                        required
                        onChange={(event) => setFilterValue(event.target.value)}
                      />
                    )}
                  </label>
                </>
              )}
              <label className="field database-analysis-limit">
                <span className="field-label">Maximum result rows</span>
                <SelectInput value={limit} onChange={(event) => setLimit(Number(event.target.value))}>
                  {[10, 25, 50, 100].map((count) => <option key={count} value={count}>{count}</option>)}
                </SelectInput>
              </label>
              <button className="btn btn-primary btn-sm" type="submit" disabled={queryBusy || !selectedTable}>
                {queryBusy ? <Spinner size={13} /> : null} Run read-only analysis
              </button>
            </form>
          )}
          {error && <p className="database-analysis-error" role="alert">{error}</p>}
          {result && (
            <div className="database-analysis-result" aria-live="polite">
              <p className="database-analysis-provenance">
                {result.provenance} · {result.returned_rows} {result.returned_rows === 1 ? "row" : "rows"}
                {result.truncated ? ` · limited to ${result.row_limit}` : ""}
              </p>
              <button
                className="btn btn-ghost btn-sm"
                type="button"
                onClick={() => void exportResult()}
                disabled={exportBusy || !resultQuery}
              >
                {exportBusy ? <Spinner size={13} /> : null} Export this result as CSV
              </button>
              {result.truncated && <p className="database-analysis-status">More results exist; narrow the query or increase the result limit.</p>}
              <div className="database-analysis-table-wrap">
                <table className="database-analysis-table">
                  <thead><tr>{result.columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr></thead>
                  <tbody>
                    {result.rows.length === 0
                      ? <tr><td colSpan={Math.max(1, result.columns.length)}>No matching rows.</td></tr>
                      : result.rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={`${rowIndex}-${cellIndex}`}>{cell}</td>)}</tr>)}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
