import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { ocrTestLabel } from "../embeddingModelPresentation.js";
import { IconAlert, Spinner } from "../icons";
import type { OcrEngineStatus, OcrEngineTestResult } from "../types";
import { Activity, ScanText } from "lucide-react";
import { Button, Card, CardBody, CardHeader } from "./ui";

export function OcrEngineSection() {
  const [status, setStatus] = useState<OcrEngineStatus | null>(null);
  const [result, setResult] = useState<OcrEngineTestResult | null>(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let mounted = true;
    void api.ocrEngineStatus().then((value) => {
      if (mounted) setStatus(value);
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  async function runHealthTest() {
    setTesting(true);
    setResult(null);
    setError(null);
    try {
      setResult(await api.testOcrEngine());
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setTesting(false);
    }
  }

  return (
    <Card className="overflow-hidden" aria-labelledby="ocr-engine-title">
      <CardHeader
        icon={<ScanText />}
        title={<span id="ocr-engine-title">Text recognition (OCR)</span>}
        description="Reads text from scanned pages and images on this server, without sending them anywhere."
        actions={<Button variant="primary" size="sm" loading={testing} onClick={() => void runHealthTest()} disabled={loading || testing}>{!testing && <Activity />} Run health test</Button>}
      />
      <CardBody className="grid gap-4">

      {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
      {loading ? (
        <div className="loading"><Spinner size={22} /></div>
      ) : status ? (
        <>
          <div className="stats ocr-engine-stats">
            <Stat label="Current engine" value={status.engine_name} />
            <Stat label="Status" value={status.enabled ? "Enabled" : "Disabled"} />
            <Stat label="Installed runtime" value={status.runtime_available ? "Available" : "Unavailable"} />
            <Stat label="Backend" value={`${status.backend} · ${status.device.toUpperCase()}`} />
          </div>
          <div className="ocr-engine-details">
            <div>
              <h3>Configuration</h3>
              <ul>
                <li>Scanned-page OCR fallback: {status.enabled ? "enabled" : "disabled"}</li>
                <li>Full-page OCR: {status.full_page_ocr_enabled ? "enabled" : "disabled"}</li>
                <li>Orientation classifier: {status.orientation_classifier_enabled ? "enabled" : "disabled"}</li>
                <li>Recognition language: {status.configured_languages.join(", ")}</li>
              </ul>
            </div>
            <div>
              <h3>Supported capabilities</h3>
              <ul>{status.capabilities.map((capability) => <li key={capability}>{capability}</li>)}</ul>
            </div>
          </div>
          <p className="muted ocr-runtime-packages">
            Runtime packages · Docling: {status.docling_available ? "available" : "missing"} · RapidOCR: {status.rapidocr_available ? "available" : "missing"} · ONNX Runtime: {status.onnxruntime_available ? "available" : "missing"} · PyTorch: {status.pytorch_available ? "available" : "missing"}
          </p>
        </>
      ) : null}

      {testing && <div className="loading" role="status">Running a synthetic scanned PDF through OCR isolation…</div>}
      {result && (
        <div className={`inline-alert ocr-test-result ${result.ok ? "inline-alert-success" : "inline-alert-warning"}`} role="status">
          <span><strong>{ocrTestLabel(result)}</strong> · {result.message}</span>
          <span>{result.latency_ms} ms · {result.parser_used}{result.page_count !== null ? ` · ${result.page_count} page` : ""}{result.recognized_characters ? ` · ${result.recognized_characters} characters` : ""}</span>
          {result.error && result.error !== result.message && <span>{result.error}</span>}
        </div>
      )}
    </CardBody>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="stat">
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
    </div>
  );
}
