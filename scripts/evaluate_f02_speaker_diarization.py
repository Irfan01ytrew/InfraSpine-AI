"""Evaluate a license-cleared offline speaker encoder on locally synthesized turns.

This is a bounded evaluation harness, not a production diarization claim. It uses
Resemblyzer's published Apache-2.0 encoder weights and source from its official
PyPI wheel. The samples are pre-segmented Windows SAPI speech, so this script
measures speaker clustering on clean, known turn boundaries; it does not measure
speech segmentation, overlap handling, ASR, or real-meeting accuracy.

The encoder needs only PyTorch, NumPy, and SciPy here. A matching Slaney mel
front-end is implemented locally so evaluation does not need librosa, WebRTC VAD,
network access, or model terms. Production integration would need separate
qualification and dependency review.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import sys
import types
import zipfile
from pathlib import Path

import numpy as np
import torch
from scipy.signal import resample_poly, windows
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import adjusted_rand_score


SAMPLE_RATE = 16_000
N_FFT = 400
HOP_LENGTH = 160
MEL_CHANNELS = 40


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hz_to_mel(frequencies: np.ndarray) -> np.ndarray:
    # Slaney's piecewise mel scale, matching librosa's default htk=False.
    linear_spacing = 200.0 / 3.0
    minimum_log_hz = 1000.0
    minimum_log_mel = minimum_log_hz / linear_spacing
    log_step = math.log(6.4) / 27.0
    frequencies = np.asarray(frequencies, dtype=np.float64)
    mel = frequencies / linear_spacing
    logarithmic = frequencies >= minimum_log_hz
    mel[logarithmic] = minimum_log_mel + np.log(frequencies[logarithmic] / minimum_log_hz) / log_step
    return mel


def _mel_to_hz(mels: np.ndarray) -> np.ndarray:
    linear_spacing = 200.0 / 3.0
    minimum_log_hz = 1000.0
    minimum_log_mel = minimum_log_hz / linear_spacing
    log_step = math.log(6.4) / 27.0
    mels = np.asarray(mels, dtype=np.float64)
    frequencies = mels * linear_spacing
    logarithmic = mels >= minimum_log_mel
    frequencies[logarithmic] = minimum_log_hz * np.exp(log_step * (mels[logarithmic] - minimum_log_mel))
    return frequencies


def _mel_filterbank() -> np.ndarray:
    mel_edges = _mel_to_hz(np.linspace(_hz_to_mel(np.array([0.0]))[0], _hz_to_mel(np.array([SAMPLE_RATE / 2]))[0], MEL_CHANNELS + 2))
    fft_frequencies = np.linspace(0, SAMPLE_RATE / 2, 1 + N_FFT // 2)
    ramps = mel_edges[:, None] - fft_frequencies[None, :]
    widths = np.diff(mel_edges)
    lower = -ramps[:-2] / widths[:-1, None]
    upper = ramps[2:] / widths[1:, None]
    weights = np.maximum(0.0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_edges[2:] - mel_edges[:-2]))[:, None]
    return weights.astype(np.float32)


MEL_FILTERBANK = _mel_filterbank()


def _wav_to_mel_spectrogram(wav: np.ndarray) -> np.ndarray:
    wav = np.asarray(wav, dtype=np.float32)
    if wav.ndim != 1 or len(wav) < N_FFT:
        raise ValueError("Each synthetic utterance must be mono and at least 25 ms long.")
    padded = np.pad(wav, (N_FFT // 2, N_FFT // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP_LENGTH]
    spectrum = np.fft.rfft(frames * windows.hann(N_FFT, sym=False), n=N_FFT, axis=1)
    power = np.abs(spectrum) ** 2
    mel = power @ MEL_FILTERBANK.T
    return mel.astype(np.float32)


def _load_encoder_from_wheel(wheel: Path, extraction_root: Path):
    if extraction_root.exists():
        raise FileExistsError(f"Refusing to overwrite existing model extraction: {extraction_root}")
    package_root = extraction_root / "resemblyzer"
    package_root.mkdir(parents=True)
    selected = (
        "resemblyzer/hparams.py",
        "resemblyzer/voice_encoder.py",
        "resemblyzer/pretrained.pt",
        "Resemblyzer-0.1.4.dist-info/LICENSE",
        "Resemblyzer-0.1.4.dist-info/METADATA",
    )
    with zipfile.ZipFile(wheel) as archive:
        available = set(archive.namelist())
        if not set(selected).issubset(available):
            raise ValueError("The local wheel is not the expected Resemblyzer 0.1.4 package.")
        for name in selected:
            target = package_root / Path(name).name if name.startswith("resemblyzer/") else extraction_root / Path(name).name
            target.write_bytes(archive.read(name))

    package = types.ModuleType("resemblyzer")
    package.__path__ = [str(package_root)]
    package.__package__ = "resemblyzer"
    sys.modules["resemblyzer"] = package
    audio = types.ModuleType("resemblyzer.audio")
    audio.wav_to_mel_spectrogram = _wav_to_mel_spectrogram
    package.audio = audio
    sys.modules["resemblyzer.audio"] = audio
    encoder_type = importlib.import_module("resemblyzer.voice_encoder").VoiceEncoder
    encoder = encoder_type(device="cpu", verbose=False, weights_fpath=package_root / "pretrained.pt")
    encoder.eval()
    return encoder, package_root / "pretrained.pt"


def _load_wav(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as audio:
        channels = audio.getnchannels()
        width = audio.getsampwidth()
        rate = audio.getframerate()
        frames = audio.readframes(audio.getnframes())
    if width != 2:
        raise ValueError(f"Expected 16-bit PCM in {path.name}.")
    samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:
        divisor = math.gcd(rate, SAMPLE_RATE)
        samples = resample_poly(samples, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)
    frame = max(1, int(SAMPLE_RATE * 0.03))
    energy = np.array(
        [np.sqrt(np.mean(samples[start : start + frame] ** 2)) for start in range(0, len(samples), frame)],
        dtype=np.float32,
    )
    peak = float(np.max(energy)) if len(energy) else 0.0
    active = np.flatnonzero(energy >= max(0.006, peak * 0.04))
    if len(active):
        margin = int(0.15 * SAMPLE_RATE)
        first = max(0, int(active[0]) * frame - margin)
        last = min(len(samples), (int(active[-1]) + 1) * frame + margin)
        samples = samples[first:last]
    rms = float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.0
    if rms < 1e-6:
        raise ValueError(f"No audible speech in {path.name}.")
    samples *= min(5.0, (10 ** (-30.0 / 20.0)) / rms)
    return samples.astype(np.float32)


def _best_mapped_accuracy(truth: list[str], predicted: np.ndarray) -> float:
    truth_labels = sorted(set(truth))
    pred_labels = sorted(set(int(label) for label in predicted))
    if len(truth_labels) != 2 or len(pred_labels) != 2:
        return 0.0
    best = 0
    for mapping in ((pred_labels[0], pred_labels[1]), (pred_labels[1], pred_labels[0])):
        mapped = [truth_labels[mapping.index(int(label))] for label in predicted]
        best = max(best, sum(a == b for a, b in zip(truth, mapped, strict=True)))
    return best / len(truth)


def evaluate(manifest_path: Path, wheel_path: Path, output_path: Path) -> dict:
    if output_path.exists():
        raise FileExistsError(f"Refusing to overwrite existing evidence: {output_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    turns = manifest.get("turns")
    speakers = sorted({str(turn["speaker"]) for turn in turns or []})
    if not turns or len(speakers) != 2 or len(turns) < 8:
        raise ValueError("Evaluation requires at least eight utterances from exactly two synthetic speakers.")

    encoder, model_path = _load_encoder_from_wheel(wheel_path, output_path.parent / "resemblyzer-0.1.4-local")
    torch.set_num_threads(1)
    embeddings = []
    durations = []
    for turn in turns:
        wav_path = manifest_path.parent / turn["file"]
        wav = _load_wav(wav_path)
        if len(wav) < int(1.6 * SAMPLE_RATE):
            raise ValueError(f"Utterance is too short for one encoder window: {wav_path.name}.")
        with torch.inference_mode():
            embeddings.append(encoder.embed_utterance(wav))
        durations.append(round(len(wav) / SAMPLE_RATE, 3))

    vectors = np.asarray(embeddings, dtype=np.float32)
    truth = [str(turn["speaker"]) for turn in turns]
    predicted = AgglomerativeClustering(n_clusters=2, metric="cosine", linkage="average").fit_predict(vectors)
    same_scores = []
    cross_scores = []
    nearest_matches = 0
    for index, embedding in enumerate(vectors):
        similarities = vectors @ embedding
        similarities[index] = -np.inf
        nearest = int(np.argmax(similarities))
        nearest_matches += truth[nearest] == truth[index]
        for other in range(index + 1, len(vectors)):
            score = float(np.dot(embedding, vectors[other]))
            (same_scores if truth[index] == truth[other] else cross_scores).append(score)

    def mean(values: list[float]) -> float:
        return round(float(np.mean(values)), 5) if values else 0.0

    result = {
        "evaluation": "synthetic_clean_turn_clustering",
        "model": "Resemblyzer 0.1.4 pretrained speaker encoder",
        "model_license": "Apache-2.0",
        "model_sha256": _sha256(model_path),
        "wheel_sha256": _sha256(wheel_path),
        "local_only": True,
        "synthetic_generator": manifest.get("generator", "Windows SAPI"),
        "turn_count": len(turns),
        "synthetic_speaker_count": len(speakers),
        "duration_seconds": durations,
        "pairwise_cosine_same_speaker_mean": mean(same_scores),
        "pairwise_cosine_cross_speaker_mean": mean(cross_scores),
        "same_minus_cross_margin": round(mean(same_scores) - mean(cross_scores), 5),
        "nearest_neighbor_turn_accuracy": round(nearest_matches / len(turns), 5),
        "agglomerative_cluster_adjusted_rand_score": round(float(adjusted_rand_score(truth, predicted)), 5),
        "cluster_label_invariant_accuracy": round(_best_mapped_accuracy(truth, predicted), 5),
        "limitations": [
            "Known clean utterance boundaries were provided; speech activity detection was not evaluated.",
            "Synthetic Windows text-to-speech voices do not represent real meeting speakers, accents, rooms, or microphones.",
            "No overlapping speech, short-turn recovery, unknown speaker count, transcript alignment, or diarization error rate was measured.",
            "This result does not authorize automatic speaker identity attribution or establish production readiness.",
        ],
    }
    output_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(args.manifest.resolve(), args.wheel.resolve(), args.output.resolve())
    summary = {key: value for key, value in result.items() if key not in {"limitations", "duration_seconds"}}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
