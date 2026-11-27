"""Synthetic parser and file-signature tests; no customer media or speech model required."""

import importlib.machinery
import sys
import uuid
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from app.rag.parsers.base import ParseError, ParseLimits, TextBudget
from app.rag.parsers.detect import detect_format
from app.rag.parsers.registry import parse_document
from app.rag.answer import parse_answer
from app.rag.retrieval import RetrievedChunk


def test_default_video_frame_interval_captures_short_clip_changes():
    from app.core.config import Settings

    assert Settings.model_fields["rag_media_video_frame_interval_seconds"].default == 2


def test_local_ocr_respects_configured_cpu_thread_budget(monkeypatch):
    import app.core.config
    from app.rag.ocr import _configure_cpu_threads

    cv2 = ModuleType("cv2")
    cv2.thread_count = None
    cv2.setNumThreads = lambda count: setattr(cv2, "thread_count", count)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setattr(app.core.config, "get_settings", lambda: SimpleNamespace(rag_ocr_cpu_threads=2))

    _configure_cpu_threads()

    assert cv2.thread_count == 2


@pytest.mark.parametrize(
    ("extension", "signature", "format_name"),
    [
        (".wav", b"RIFF\x00\x00\x00\x00WAVE", "audio"),
        (".mp3", b"ID3\x04\x00\x00", "audio"),
        (".flac", b"fLaC\x00\x00", "audio"),
        (".mp4", b"\x00\x00\x00\x18ftypisom", "video"),
        (".mkv", b"\x1a\x45\xdf\xa3\x93\x42", "video"),
        (".avi", b"RIFF\x00\x00\x00\x00AVI ", "video"),
    ],
)
def test_media_file_signatures_match_supported_extensions(tmp_path, extension, signature, format_name):
    path = tmp_path / f"synthetic{extension}"
    path.write_bytes(signature)
    assert detect_format(path, format_name, 1024) == format_name


def test_media_signature_rejects_renamed_or_text_input(tmp_path):
    path = tmp_path / "renamed.mp4"
    path.write_text("this is not an mp4", encoding="utf-8")
    with pytest.raises(ParseError):
        detect_format(path, "video", 1024)


def test_audio_transcription_produces_timestamped_citation_sections(tmp_path, monkeypatch):
    import numpy as np

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    settings = SimpleNamespace(
        rag_media_enabled=True,
        rag_media_model_path=str(model_path),
        rag_media_max_file_bytes=1024 * 1024,
        rag_media_max_file_mb=1,
        rag_media_device="cpu",
        rag_media_cpu_threads=2,
        rag_media_max_duration_seconds=120,
        rag_media_segment_seconds=30,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    class Frame:
        samples = 16_000

        def to_ndarray(self):
            return np.zeros((1, self.samples), dtype=np.int16)

    class Resampler:
        def __init__(self, **_kwargs):
            pass

        def resample(self, frame):
            return [frame] if frame is not None else []

    class Container:
        duration = 2_000_000
        streams = SimpleNamespace(audio=[object()])

        def decode(self, _stream):
            yield Frame()

        def close(self):
            pass

    av_module = ModuleType("av")
    av_module.time_base = 1_000_000
    av_module.AudioResampler = Resampler
    av_module.open = lambda *_args, **_kwargs: Container()

    class WhisperModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            segment = SimpleNamespace(start=0.25, end=0.8, text="Synthetic maintenance checklist.")
            return iter([segment]), SimpleNamespace(language="en")

    whisper_module = ModuleType("faster_whisper")
    whisper_module.WhisperModel = WhisperModel
    monkeypatch.setitem(sys.modules, "av", av_module)
    monkeypatch.setitem(sys.modules, "faster_whisper", whisper_module)
    monkeypatch.setattr(
        "app.rag.parsers.media_parser.importlib.util.find_spec",
        lambda name: importlib.machinery.ModuleSpec(name, loader=None),
    )

    media = tmp_path / "meeting.wav"
    media.write_bytes(b"RIFF\x00\x00\x00\x00WAVE")
    sections = parse_document(
        media,
        "audio",
        ParseLimits(max_units=100, max_chars=10_000, max_unpacked_bytes=1024, section_chars=800),
        use_docling=False,
    )

    assert len(sections) == 1
    assert sections[0].text == "Synthetic maintenance checklist."
    assert sections[0].locator.kind == "time"
    assert sections[0].locator.label == "00:00:00.250–00:00:00.800"
    assert sections[0].locator.attrs["start_seconds"] == 0.25
    assert sections[0].locator.attrs["end_seconds"] == 0.8

    passage = RetrievedChunk(
        chunk_id=1,
        document_id=uuid.uuid4(),
        filename="meeting.wav",
        page_number=sections[0].locator.position,
        chunk_index=0,
        content=sections[0].text,
        score=0.9,
        locator=sections[0].locator.to_json(),
    )
    answer, insufficient, citations = parse_answer("The meeting contains a maintenance checklist [S1].", [passage])
    assert not insufficient
    assert "[S1]" in answer
    assert citations[0][1].location_label == "00:00:00.250–00:00:00.800"
    assert not citations[0][1].is_page


def test_video_frame_ocr_is_local_bounded_and_timestamped(tmp_path, monkeypatch):
    import numpy as np

    from app.rag.parsers.media_parser import _video_frame_text

    class Stream:
        time_base = 0.001

    class Frame:
        def __init__(self, timestamp):
            self.time = timestamp
            self.width = 4000
            self.height = 2000

        def reformat(self, *, width, height, format):
            assert format == "rgb24"
            assert max(width, height) == 1280
            self.width = width
            self.height = height
            return self

        def to_ndarray(self, *, format):
            assert format == "rgb24"
            return np.zeros((self.height, self.width, 3), dtype=np.uint8)

    class Container:
        duration = 90_000_000
        streams = SimpleNamespace(video=[Stream()])

        def seek(self, offset, *, stream, backward, any_frame):
            assert backward is True and any_frame is False
            self.target = offset * stream.time_base

        def decode(self, _stream):
            yield Frame(float(self.target))

        def close(self):
            pass

    class FakeAv:
        time_base = 1_000_000

        @staticmethod
        def open(*_args, **_kwargs):
            return Container()

    class FakeOcr:
        def __init__(self):
            self.frames = 0

        def __call__(self, _pixels):
            self.frames += 1
            return SimpleNamespace(txts=[f"synthetic screen label {self.frames}"])

    fake_ocr = FakeOcr()
    monkeypatch.setattr("app.rag.ocr._local_engine", lambda: fake_ocr)
    settings = SimpleNamespace(
        rag_media_video_frame_ocr_enabled=True,
        rag_media_video_frame_interval_seconds=30,
        rag_media_video_max_frames=3,
    )
    budget = TextBudget(1000)
    sections = _video_frame_text(
        tmp_path / "synthetic.mp4",
        settings,
        ParseLimits(max_units=100, max_chars=1000, max_unpacked_bytes=1024, section_chars=800),
        FakeAv,
        budget,
    )

    assert [section.locator.kind for section in sections] == ["video_frame"] * 3
    assert [section.locator.attrs["timestamp_seconds"] for section in sections] == [0, 30, 60]
    assert all("synthetic screen label" in section.text for section in sections)
    assert budget.used == sum(len(f"synthetic screen label {index}") for index in range(1, 4))


def test_real_video_decode_runs_frame_ocr_and_keeps_timestamp_citations(tmp_path, monkeypatch):
    """Exercise PyAV decoding on a generated video while keeping recognition deterministic and offline."""
    import importlib.util

    if importlib.util.find_spec("av") is None:
        pytest.skip("local PyAV video decoder is unavailable")

    import av
    import numpy as np

    from app.rag.parsers.media_parser import VideoParser

    model_path = tmp_path / "local-speech-model"
    model_path.mkdir()
    settings = SimpleNamespace(
        rag_media_enabled=True,
        rag_media_model_path=str(model_path),
        rag_media_max_file_bytes=10 * 1024 * 1024,
        rag_media_max_file_mb=10,
        rag_media_device="cpu",
        rag_media_cpu_threads=1,
        rag_media_max_duration_seconds=10,
        rag_media_segment_seconds=3,
        rag_media_video_frame_ocr_enabled=True,
        rag_media_video_frame_interval_seconds=1,
        rag_media_video_max_frames=3,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    class UnusedSpeechModel:
        def __init__(self, *_args, **_kwargs):
            pass

    class FakeOcr:
        def __init__(self):
            self.calls = 0

        def __call__(self, _pixels):
            self.calls += 1
            return SimpleNamespace(txts=[f"actual decoded frame {self.calls} 7{self.calls}31"])

    fake_ocr = FakeOcr()
    monkeypatch.setattr("faster_whisper.WhisperModel", UnusedSpeechModel)
    monkeypatch.setattr("app.rag.ocr._local_engine", lambda: fake_ocr)
    video_path = tmp_path / "synthetic-decoder-test.mp4"
    output = av.open(str(video_path), mode="w", format="mp4")
    try:
        try:
            stream = output.add_stream("mpeg4", rate=1)
        except Exception as exc:
            pytest.skip(f"local PyAV MPEG-4 encoder unavailable: {type(exc).__name__}")
        stream.width, stream.height, stream.pix_fmt = 320, 240, "yuv420p"
        for _ in range(3):
            frame = av.VideoFrame.from_ndarray(np.zeros((240, 320, 3), dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    finally:
        output.close()

    sections = VideoParser().parse(
        video_path,
        ParseLimits(max_units=3, max_chars=1000, max_unpacked_bytes=1024, section_chars=500),
    )
    assert fake_ocr.calls == 3
    assert [section.locator.kind for section in sections] == ["video_frame"] * 3
    assert [section.locator.attrs["timestamp_seconds"] for section in sections] == [0, 1, 2]
    assert all("actual decoded frame" in section.text for section in sections)

    passage = RetrievedChunk(
        chunk_id=9,
        document_id=uuid.uuid4(),
        filename=video_path.name,
        page_number=sections[1].locator.position,
        chunk_index=1,
        content=sections[1].text,
        score=0.9,
        locator=sections[1].locator.to_json(),
    )
    answer, insufficient, citations = parse_answer("The second frame shows label 7731 [S1].", [passage])
    assert not insufficient
    assert "[S1]" in answer
    assert citations[0][1].location_label == "Frame at 00:00:01.000"
    assert not citations[0][1].is_page


def test_video_frame_ocr_uses_staged_local_weights_on_synthetic_video(tmp_path, monkeypatch):
    """Exercise the actual local OCR model without network access or customer media."""
    import importlib.util

    if any(importlib.util.find_spec(name) is None for name in ("av", "cv2", "rapidocr", "onnxruntime")):
        pytest.skip("local PyAV/OpenCV/RapidOCR ONNX runtime is unavailable")

    import av
    import cv2
    import numpy as np

    rapidocr_spec = importlib.util.find_spec("rapidocr")
    assert rapidocr_spec is not None and rapidocr_spec.origin is not None
    model_dir = Path(rapidocr_spec.origin).resolve().parent / "models"
    required_models = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx")
    if any(not (model_dir / name).is_file() for name in required_models):
        pytest.skip("verified local RapidOCR ONNX weights are not staged")

    from app.rag.parsers.media_parser import _video_frame_text

    monkeypatch.setattr("app.core.config.get_settings", lambda: SimpleNamespace(rag_ocr_cpu_threads=1))
    settings = SimpleNamespace(
        rag_media_video_frame_ocr_enabled=True,
        rag_media_video_frame_interval_seconds=1,
        rag_media_video_max_frames=2,
    )
    video_path = tmp_path / "synthetic-local-ocr.mp4"
    output = av.open(str(video_path), mode="w", format="mp4")
    try:
        stream = output.add_stream("mpeg4", rate=1)
        stream.width, stream.height, stream.pix_fmt = 960, 540, "yuv420p"
        image = np.full((540, 960, 3), 255, dtype=np.uint8)
        cv2.putText(
            image,
            "SAFETY EXIT 7319",
            (55, 300),
            cv2.FONT_HERSHEY_SIMPLEX,
            3.2,
            (0, 0, 0),
            7,
            cv2.LINE_AA,
        )
        frame = av.VideoFrame.from_ndarray(image, format="rgb24")
        for packet in stream.encode(frame):
            output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    finally:
        output.close()

    sections = _video_frame_text(
        video_path,
        settings,
        ParseLimits(max_units=2, max_chars=1000, max_unpacked_bytes=1024, section_chars=800),
        av,
        TextBudget(1000),
    )
    recognized = " ".join(section.text for section in sections)
    assert "7319" in recognized
    assert sections[0].locator.kind == "video_frame"
    assert sections[0].locator.attrs["timestamp_seconds"] == 0


def test_disabled_video_ocr_does_not_open_or_decode_frames(monkeypatch):
    from app.rag.parsers.media_parser import _video_frame_text

    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled video OCR must not initialize OCR or open the media")

    monkeypatch.setattr("app.rag.ocr._local_engine", forbidden)
    settings = SimpleNamespace(rag_media_video_frame_ocr_enabled=False)
    sections = _video_frame_text(
        Path("unused.mp4"), settings,
        ParseLimits(max_units=10, max_chars=100, max_unpacked_bytes=100, section_chars=50),
        SimpleNamespace(open=forbidden), TextBudget(100),
    )
    assert sections == []


def test_video_parser_requires_optional_local_decoder(tmp_path, monkeypatch):
    from app.rag.parsers.media_parser import _configuration

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    settings = SimpleNamespace(rag_media_enabled=True, rag_media_model_path=str(model_path))
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    monkeypatch.setattr(
        "app.rag.parsers.media_parser.importlib.util.find_spec",
        lambda name: None if name == "av" else importlib.machinery.ModuleSpec(name, loader=None),
    )
    with pytest.raises(ParseError, match="local media decoding runtime") as error:
        _configuration()
    assert error.value.code == "media_runtime_unavailable"


def test_video_ocr_real_decode_and_local_recognition(tmp_path, monkeypatch):
    """Integration path: generate a local video, decode it with PyAV, and recognize visible text with local OCR."""
    import importlib.util

    for module in ("av", "faster_whisper", "rapidocr", "onnxruntime"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"local video OCR integration dependency is unavailable: {module}")

    try:
        import av
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont

        import rapidocr
    except ImportError as exc:
        pytest.skip(f"local video OCR integration dependency is unavailable: {type(exc).__name__}")

    model_dir = Path(rapidocr.__file__).resolve().parent / "models"
    # The ONNX recognizer embeds its character set; only the detector and recognizer
    # weights need to be staged for this fully offline integration test.
    required_models = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx")
    if any(not (model_dir / name).is_file() for name in required_models):
        pytest.skip("local RapidOCR model weights are not staged")

    video_path = tmp_path / "synthetic-visible-text.mp4"
    labels = ("FRAME ALPHA 4821", "FRAME BRAVO 5317", "FRAME CHARLIE 6402")
    output = av.open(str(video_path), mode="w", format="mp4")
    try:
        stream = output.add_stream("mpeg4", rate=1)
    except Exception as exc:
        output.close()
        pytest.skip(f"local PyAV MPEG-4 encoder unavailable: {type(exc).__name__}")
    try:
        stream.width = 960
        stream.height = 540
        stream.pix_fmt = "yuv420p"
        font = ImageFont.load_default(size=76)
        for label in labels:
            image = Image.new("RGB", (960, 540), "white")
            draw = ImageDraw.Draw(image)
            bounds = draw.textbbox((0, 0), label, font=font, stroke_width=2)
            x = (960 - (bounds[2] - bounds[0])) // 2
            y = (540 - (bounds[3] - bounds[1])) // 2
            draw.text((x, y), label, font=font, fill="black", stroke_width=2, stroke_fill="black")
            frame = av.VideoFrame.from_ndarray(np.asarray(image), format="rgb24")
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    finally:
        output.close()

    model_path = tmp_path / "unused-local-speech-model"
    model_path.mkdir()
    settings = SimpleNamespace(
        rag_media_enabled=True,
        rag_media_model_path=str(model_path),
        rag_media_max_file_bytes=10 * 1024 * 1024,
        rag_media_max_file_mb=10,
        rag_media_device="cpu",
        rag_media_cpu_threads=1,
        rag_media_max_duration_seconds=10,
        rag_media_segment_seconds=3,
        rag_media_video_frame_ocr_enabled=True,
        rag_media_video_frame_interval_seconds=1,
        rag_media_video_max_frames=3,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    class UnusedSpeechModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            return iter(()), SimpleNamespace(language="und")

    monkeypatch.setattr("faster_whisper.WhisperModel", UnusedSpeechModel)
    sections = parse_document(
        video_path,
        "video",
        ParseLimits(max_units=3, max_chars=1000, max_unpacked_bytes=1024, section_chars=500),
        use_docling=False,
    )

    assert [section.locator.attrs["timestamp_seconds"] for section in sections] == [0, 1, 2]
    recognized = " ".join(section.text for section in sections).casefold()
    assert all(number in recognized for number in ("4821", "5317", "6402"))


def test_corrupt_video_container_returns_safe_parser_error(tmp_path, monkeypatch):
    import importlib.util

    for module in ("av", "faster_whisper"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"local video decoder dependency is unavailable: {module}")

    from app.rag.parsers.media_parser import VideoParser

    model_path = tmp_path / "local-model"
    model_path.mkdir()
    settings = SimpleNamespace(
        rag_media_enabled=True,
        rag_media_model_path=str(model_path),
        rag_media_max_file_bytes=1024,
        rag_media_max_file_mb=1,
    )
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)

    class UnusedSpeechModel:
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setattr("faster_whisper.WhisperModel", UnusedSpeechModel)
    corrupt = tmp_path / "corrupt.mp4"
    corrupt.write_bytes(b"\x00\x00\x00\x18ftypisomnot-a-video")
    with pytest.raises(ParseError) as error:
        VideoParser().parse(
            corrupt,
            ParseLimits(max_units=3, max_chars=1000, max_unpacked_bytes=1024, section_chars=500),
        )
    assert error.value.code == "invalid_media"
