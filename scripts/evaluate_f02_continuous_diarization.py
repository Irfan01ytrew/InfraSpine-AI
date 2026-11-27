"""Run a bounded end-to-end offline diarization check on synthetic speech turns.

The corpus is locally generated Windows SAPI speech with 800 ms inter-turn silence.
An energy VAD finds turns without reference boundaries; the local Apache-2.0
Resemblyzer encoder and agglomerative clustering assign speaker clusters. The
evaluation uses oracle speaker count (two) and reports a 250 ms collar DER. This
is reproducible synthetic qualification evidence, not a production diarizer.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import itertools
import json
import math
import sys
import types
import wave
from pathlib import Path

import numpy as np
import torch
from sklearn.cluster import AgglomerativeClustering

import evaluate_f02_speaker_diarization as baseline


SAMPLE_RATE = baseline.SAMPLE_RATE
FRAME_MS = 20
FRAME = SAMPLE_RATE * FRAME_MS // 1000
SILENCE_SECONDS = 0.8


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_local_encoder(model_dir: Path):
    package_root = model_dir / "resemblyzer"
    model_path = package_root / "pretrained.pt"
    license_path = model_dir / "LICENSE"
    if not model_path.is_file() or not license_path.is_file():
        raise FileNotFoundError("The already reviewed local encoder and license evidence are required.")
    if "apache license" not in license_path.read_text(encoding="utf-8", errors="replace").casefold():
        raise ValueError("The local speaker encoder license evidence did not identify Apache-2.0.")

    package = types.ModuleType("resemblyzer")
    package.__path__ = [str(package_root)]
    package.__package__ = "resemblyzer"
    sys.modules["resemblyzer"] = package
    audio = types.ModuleType("resemblyzer.audio")
    audio.wav_to_mel_spectrogram = baseline._wav_to_mel_spectrogram
    package.audio = audio
    sys.modules["resemblyzer.audio"] = audio
    encoder_type = importlib.import_module("resemblyzer.voice_encoder").VoiceEncoder
    torch.set_num_threads(1)
    encoder = encoder_type(device="cpu", verbose=False, weights_fpath=model_path)
    encoder.eval()
    return encoder, model_path, license_path


def detect_turns(wav: np.ndarray) -> tuple[list[tuple[int, int]], float]:
    frames = np.lib.stride_tricks.sliding_window_view(wav, FRAME)[::FRAME]
    rms = np.sqrt(np.mean(frames.astype(np.float64) ** 2, axis=1))
    peak = float(np.percentile(rms[rms > 0], 95)) if np.any(rms > 0) else 0.0
    threshold = max(0.0012, peak * 0.035)
    active = rms >= threshold

    # Fill short within-turn voice pauses while keeping the synthetic 400 ms gap between turns.
    max_gap = int(0.55 * 1000 / FRAME_MS)
    indexes = np.flatnonzero(active)
    for left, right in zip(indexes, indexes[1:]):
        if 1 < right - left <= max_gap + 1:
            active[left : right + 1] = True

    changes = np.diff(np.pad(active.astype(np.int8), (1, 1)))
    starts = np.flatnonzero(changes == 1)
    ends = np.flatnonzero(changes == -1)
    pad = int(0.06 * SAMPLE_RATE)
    segments = []
    for start_frame, end_frame in zip(starts, ends, strict=True):
        start = max(0, int(start_frame) * FRAME - pad)
        end = min(len(wav), int(end_frame) * FRAME + pad)
        if end - start >= int(1.45 * SAMPLE_RATE):
            segments.append((start, end))
    return segments, threshold


def reference_metrics(
    total_samples: int,
    references: list[tuple[int, int, str]],
    predictions: list[tuple[int, int, int]],
    collar_seconds: float = 0.25,
) -> dict[str, float | int]:
    frame = FRAME
    frames = math.ceil(total_samples / frame)
    ref = np.full(frames, -1, dtype=np.int8)
    pred = np.full(frames, -1, dtype=np.int8)
    speaker_names = sorted({name for _, _, name in references})
    speaker_id = {name: index for index, name in enumerate(speaker_names)}
    for start, end, name in references:
        ref[max(0, start // frame) : min(frames, math.ceil(end / frame))] = speaker_id[name]
    for start, end, label in predictions:
        pred[max(0, start // frame) : min(frames, math.ceil(end / frame))] = label

    ignored = np.zeros(frames, dtype=bool)
    collar = int(collar_seconds * SAMPLE_RATE / frame)
    for start, end, _ in references:
        for boundary in (start // frame, math.ceil(end / frame)):
            ignored[max(0, boundary - collar) : min(frames, boundary + collar + 1)] = True
    evaluated = ~ignored

    predicted_ids = sorted(int(value) for value in np.unique(pred[pred >= 0]))
    best_mapping = None
    best_correct = -1
    if len(predicted_ids) != len(speaker_names):
        mapping_candidates = [tuple(predicted_ids)]
    else:
        mapping_candidates = list(itertools.permutations(predicted_ids))
    for mapping in mapping_candidates:
        mapped = np.full(frames, -1, dtype=np.int8)
        for truth_id, predicted_id in enumerate(mapping):
            mapped[pred == predicted_id] = truth_id
        correct = int(np.sum((ref == mapped) & (ref >= 0) & evaluated))
        if correct > best_correct:
            best_correct = correct
            best_mapping = mapped
    if best_mapping is None:
        best_mapping = np.full(frames, -1, dtype=np.int8)

    reference_speech = (ref >= 0) & evaluated
    predicted_speech = (pred >= 0) & evaluated
    missed = reference_speech & ~predicted_speech
    false_alarm = ~reference_speech & predicted_speech
    confusion = reference_speech & predicted_speech & (ref != best_mapping)
    denominator = max(1, int(reference_speech.sum()))
    seconds_per_frame = frame / SAMPLE_RATE
    missed_seconds = float(missed.sum() * seconds_per_frame)
    false_alarm_seconds = float(false_alarm.sum() * seconds_per_frame)
    confusion_seconds = float(confusion.sum() * seconds_per_frame)
    reference_seconds = float(reference_speech.sum() * seconds_per_frame)
    return {
        "der_250ms_collar": round((missed.sum() + false_alarm.sum() + confusion.sum()) / denominator, 5),
        "reference_speech_seconds_after_collar": round(reference_seconds, 3),
        "missed_speech_seconds": round(missed_seconds, 3),
        "false_alarm_seconds": round(false_alarm_seconds, 3),
        "speaker_confusion_seconds": round(confusion_seconds, 3),
    }


def save_wav(path: Path, samples: np.ndarray) -> None:
    pcm = np.clip(samples, -1.0, 1.0)
    pcm16 = (pcm * 32767).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(pcm16.tobytes())


def run(input_dir: Path, output_dir: Path) -> dict:
    if output_dir.exists():
        raise FileExistsError("Refusing to overwrite existing end-to-end F02 evidence.")
    manifest_path = input_dir / "manifest.json"
    reference_evidence = input_dir / "evaluation.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    prior = json.loads(reference_evidence.read_text(encoding="utf-8-sig"))
    turns = manifest.get("turns") or []
    speakers = sorted({str(turn["speaker"]) for turn in turns})
    if len(turns) < 8 or len(speakers) != 2:
        raise ValueError("The synthetic corpus must contain at least eight turns and exactly two speakers.")

    utterances = [baseline._load_wav(input_dir / turn["file"]) for turn in turns]
    silence = np.zeros(int(SILENCE_SECONDS * SAMPLE_RATE), dtype=np.float32)
    parts: list[np.ndarray] = []
    references = []
    cursor = 0
    for index, (turn, utterance) in enumerate(zip(turns, utterances, strict=True)):
        if index:
            parts.append(silence)
            cursor += len(silence)
        start = cursor
        parts.append(utterance)
        cursor += len(utterance)
        references.append((start, cursor, str(turn["speaker"])))
    combined = np.concatenate(parts).astype(np.float32)
    encoder, model_path, license_path = load_local_encoder(input_dir / "resemblyzer-0.1.4-local")
    segments, threshold = detect_turns(combined)
    if len(segments) != len(turns):
        raise RuntimeError(f"Energy VAD found {len(segments)} turns for {len(turns)} isolated synthetic turns.")

    vectors = []
    for start, end in segments:
        if end - start < int(1.6 * SAMPLE_RATE):
            raise RuntimeError("A detected synthetic turn was too short for the local encoder.")
        with torch.inference_mode():
            vectors.append(encoder.embed_utterance(combined[start:end]))
    vectors = np.asarray(vectors, dtype=np.float32)
    predicted = AgglomerativeClustering(n_clusters=2, metric="cosine", linkage="average").fit_predict(vectors)
    predictions = [(start, end, int(label)) for (start, end), label in zip(segments, predicted, strict=True)]
    metrics = reference_metrics(len(combined), references, predictions)

    start_errors = [abs(segments[index][0] - references[index][0]) / SAMPLE_RATE for index in range(len(turns))]
    end_errors = [abs(segments[index][1] - references[index][1]) / SAMPLE_RATE for index in range(len(turns))]
    evidence = {
        "evaluation": "continuous_synthetic_speech_activity_and_diarization",
        "model": "Resemblyzer 0.1.4 pretrained speaker encoder",
        "model_license": "Apache-2.0",
        "model_sha256": sha256(model_path),
        "license_sha256": sha256(license_path),
        "source_evaluation_model_sha256": prior["model_sha256"],
        "local_only": True,
        "synthetic_generator": manifest.get("generator", "Windows SAPI"),
        "turn_count_reference": len(turns),
        "turn_count_detected": len(segments),
        "speaker_count_reference": len(speakers),
        "speaker_count_given_to_clusterer": 2,
        "audio_seconds": round(len(combined) / SAMPLE_RATE, 3),
        "vad": {
            "method": "20ms RMS threshold with 550ms short-gap merge and 60ms boundary pad",
            "rms_threshold": round(threshold, 7),
            "mean_absolute_start_error_seconds": round(float(np.mean(start_errors)), 4),
            "mean_absolute_end_error_seconds": round(float(np.mean(end_errors)), 4),
        },
        **metrics,
        "known_limitations": [
            "Two clean non-overlapping speakers, known synthetic turn transcript, and controlled 800ms inter-turn silence.",
            "The clusterer received the correct speaker count; unknown speaker count was not evaluated.",
            "Energy VAD can miss quiet speech or split natural pauses; results do not qualify real meeting conditions.",
            "The 250ms collar excludes reference boundaries from the DER calculation.",
            "No real media, overlap, noise, language variation, ASR alignment, or customer data was used.",
            "This does not attribute real-world identities and is not a production-readiness claim.",
        ],
    }
    audio_path = output_dir / "synthetic-continuous-two-speaker.wav"
    evidence_path = output_dir / "evaluation.json"
    output_dir.mkdir(parents=True)
    save_wav(audio_path, combined)
    evidence["combined_audio_sha256"] = sha256(audio_path)
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.input_dir.resolve(), args.output_dir.resolve())
    print(json.dumps({key: value for key, value in result.items() if key != "known_limitations"}, indent=2))


if __name__ == "__main__":
    main()
