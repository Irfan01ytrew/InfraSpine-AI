import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { parserAvailabilityLabel, parserTestLabel } from "../embeddingModelPresentation.js";
import { IconAlert, Spinner } from "../icons";
import type { DocumentParserInfo, DocumentParserTestResult, ParserFormat } from "../types";
import { FileText } from "lucide-react";
import { Card, CardBody, CardHeader } from "./ui";

export function DocumentParsersSection() {
  const [info, setInfo] = useState<DocumentParserInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState<ParserFormat | null>(null);
  const [results, setResults] = useState<Partial<Record<ParserFormat, DocumentParserTestResult>>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    void api.documentParsers().then((value) => {
      if (mounted) setInfo(value);
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  async function runTest(format: ParserFormat) {
    setTesting(format);
    setError(null);
    try {
      const result = await api.testDocumentParser(format);
      setResults((current) => ({ ...current, [format]: result }));
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setTesting(null);
    }
  }

  return (
    <Card className="overflow-hidden" aria-labelledby="document-parsers-title">
      <CardHeader
        icon={<FileText />}
        title={<span id="document-parsers-title">Document readers</span>}
        description={<>Readers for each file type, tested with small built-in sample files in an isolated process. {info?.docling_enabled ? "Advanced layout reading (Docling) is on for PDF, Word and PowerPoint." : "Built-in readers are active."}</>}
      />
      <CardBody className="grid gap-4">

      {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
      {loading ? (
        <div className="loading"><Spinner size={22} /></div>
      ) : info ? (
        <div className="parser-table-wrap">
          <table className="parser-table">
            <thead>
              <tr>
                <th scope="col">Format</th>
                <th scope="col">Active parser</th>
                <th scope="col">Availability</th>
                <th scope="col">Fallback parser</th>
                <th scope="col">Availability</th>
                <th scope="col">Health test</th>
              </tr>
            </thead>
            <tbody>
              {info.formats.map((parser) => {
                const result = results[parser.file_format];
                return (
                  <FragmentRows
                    key={parser.file_format}
                    format={parser.file_format}
                    extension={parser.extension}
                    activeParser={parser.active_parser}
                    activeAvailable={parser.active_available}
                    fallbackParser={parser.fallback_parser}
                    fallbackAvailable={parser.fallback_available}
                    result={result}
                    testing={testing === parser.file_format}
                    disabled={testing !== null}
                    onTest={() => void runTest(parser.file_format)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}
    </CardBody>
    </Card>
  );
}

function FragmentRows({
  format,
  extension,
  activeParser,
  activeAvailable,
  fallbackParser,
  fallbackAvailable,
  result,
  testing,
  disabled,
  onTest,
}: {
  format: ParserFormat;
  extension: string;
  activeParser: string;
  activeAvailable: boolean;
  fallbackParser: string | null;
  fallbackAvailable: boolean | null;
  result?: DocumentParserTestResult;
  testing: boolean;
  disabled: boolean;
  onTest: () => void;
}) {
  return (
    <>
      <tr>
        <th scope="row">{extension.toUpperCase()}</th>
        <td>{activeParser}</td>
        <td>{parserAvailabilityLabel(activeAvailable)}</td>
        <td>{fallbackParser ?? "—"}</td>
        <td>{fallbackAvailable === null ? "—" : parserAvailabilityLabel(fallbackAvailable)}</td>
        <td>
          <button className="btn btn-sm" onClick={onTest} disabled={disabled} aria-label={`Test ${format.toUpperCase()} parser`}>
            {testing ? <><Spinner size={13} /> Testing…</> : "Test"}
          </button>
        </td>
      </tr>
      {(result || testing) && (
        <tr className="parser-result-row">
          <td colSpan={6} role="status">
            {testing ? "Running fixture in parser isolation…" : result && (
              <span className={result.ok ? "parser-result-success" : "parser-result-failure"}>
                <strong>{result.ok ? "Passed" : "Failed"}</strong> · {parserTestLabel(result)} · {result.latency_ms} ms
                {result.chunk_count !== null && ` · ${result.chunk_count} chunk${result.chunk_count === 1 ? "" : "s"}`}
                {result.page_count !== null && ` · ${result.page_count} page${result.page_count === 1 ? "" : "s"}`}
                {result.error && ` · ${result.error}`}
              </span>
            )}
          </td>
        </tr>
      )}
    </>
  );
}
