import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { availabilityLabel, loadLabel } from "../embeddingModelPresentation.js";
import { IconAlert, Spinner } from "../icons";
import type { EmbeddingModelStatus, EmbeddingModelTestResult } from "../types";
import { Activity, Cpu } from "lucide-react";
import { Button, Card, CardBody, CardHeader } from "./ui";

export function EmbeddingModelsPage() {
  const [status, setStatus] = useState<EmbeddingModelStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<EmbeddingModelTestResult | null>(null);

  useEffect(() => {
    let mounted = true;
    void api.embeddingModelStatus().then(
      (value) => {
        if (mounted) setStatus(value);
      },
    ).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => {
      mounted = false;
    };
  }, []);

  async function runHealthCheck() {
    setTesting(true);
    setResult(null);
    setError(null);
    try {
      const checked = await api.testEmbeddingModel();
      setResult(checked);
      setStatus(await api.embeddingModelStatus());
    } catch (reason) {
      setError(describeError(reason));
    } finally {
      setTesting(false);
    }
  }

  return (
    <>
      <Card className="overflow-hidden" aria-labelledby="embedding-models-title">
        <CardHeader
          icon={<Cpu />}
          title={<span id="embedding-models-title">Document understanding model</span>}
          description="The local model that turns text into searchable meaning. It is set by the server configuration."
          actions={<Button variant="primary" size="sm" loading={testing} onClick={() => void runHealthCheck()} disabled={loading || testing}>{!testing && <Activity />} Run health test</Button>}
        />
      <CardBody className="grid gap-4">

        {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
        {loading ? (
          <div className="loading"><Spinner size={22} /></div>
        ) : status ? (
          <>
            <div className="stats embedding-model-stats">
              <div className="stat">
                <span className="stat-label">Active model</span>
                <span className="stat-value" title={status.model_name}>{status.model_name}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Vector dimensions</span>
                <span className="stat-value">{status.dimensions}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Availability</span>
                <span className="stat-value">{availabilityLabel(status)}</span>
              </div>
              <div className="stat">
                <span className="stat-label">Load status</span>
                <span className="stat-value">{loadLabel(status)}</span>
              </div>
            </div>
            <p className="muted embedding-model-device">Runs on {status.device.toUpperCase()}. The health test embeds a fixed sample locally and does not download model files.</p>
          </>
        ) : null}

        {result && (
          <div className={`inline-alert ${result.ok ? "inline-alert-success" : "inline-alert-warning"}`} role="status">
            <span><strong>{result.ok ? "Health test passed" : "Health test failed"}</strong> · {result.message}</span>
            <span>{result.latency_ms} ms</span>
          </div>
        )}
      </CardBody>
    </Card>
    </>
  );
}
