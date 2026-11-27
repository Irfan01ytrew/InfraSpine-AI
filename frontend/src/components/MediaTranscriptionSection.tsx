import { useEffect, useState } from "react";
import { api, describeError } from "../api";
import { IconAlert, Spinner } from "../icons";
import type { MediaTranscriptionStatus } from "../types";
import { AudioLines } from "lucide-react";
import { Badge, Card, CardBody, CardHeader } from "./ui";

export function MediaTranscriptionSection() {
  const [status, setStatus] = useState<MediaTranscriptionStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let mounted = true;
    void api.mediaTranscriptionStatus().then((value) => {
      if (mounted) setStatus(value);
    }).catch((reason: unknown) => {
      if (mounted) setError(describeError(reason));
    }).finally(() => {
      if (mounted) setLoading(false);
    });
    return () => { mounted = false; };
  }, []);

  return <Card className="overflow-hidden" aria-labelledby="media-transcription-title">
    <CardHeader
      icon={<AudioLines />}
      title={<span id="media-transcription-title">Audio & video indexing</span>}
      description="Speech-to-text for recordings and text from video frames, processed on this server. Media and transcripts never leave it."
      actions={status && <Badge tone={status.status === "ready" ? "success" : status.status === "unavailable" ? "warning" : "neutral"} dot>{status.status}</Badge>}
    />
      <CardBody className="grid gap-4">
    {error && <div className="inline-alert inline-alert-error" role="alert"><IconAlert size={17} />{error}</div>}
    {loading ? <div className="loading"><Spinner size={22} /></div> : status ? <>
      <div className="stats">
        <Stat label="Configuration" value={status.enabled ? "Enabled" : "Disabled"} />
        <Stat label="Runtime" value={status.runtime_available ? "Installed" : "Not installed"} />
        <Stat label="Local model" value={status.model_available ? "Available" : "Not staged"} />
        <Stat label="Device" value={status.device.toUpperCase()} />
        <Stat label="Video OCR runtime" value={!status.video_frame_ocr_enabled ? "Disabled" : status.video_frame_ocr_available ? "Installed" : "Runtime missing"} />
      </div>
      <p className="muted small media-transcription-note">
        File limit {status.max_file_mb.toLocaleString()} MB · duration limit {Math.floor(status.max_duration_seconds / 3600)} hours · {status.segment_seconds}-second decode segments.
        {status.video_frame_ocr_enabled && <> Video OCR samples up to {status.video_max_frames} frames, at least {status.video_frame_interval_seconds} seconds apart. </>}
        Install <code>requirements-media.txt</code>, stage a local faster-whisper model, then set <code>RAG_MEDIA_ENABLED=true</code> and <code>RAG_MEDIA_MODEL_PATH</code>.
        Folder imports support these larger media files; direct uploads remain subject to the browser upload limit.
      </p>
    </> : null}
  </CardBody>
    </Card>;
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="stat"><span className="stat-label">{label}</span><span className="stat-value">{value}</span></div>;
}
