"""Audio/video adapter — cloud STT, opt-in + key-gated.

This is the one adapter that sends data off-host (the recording is
uploaded to a third-party STT API). It runs ONLY when given both a
recording input and an ``stt_key``. The transcription HTTP call is
isolated in ``_transcribe`` so it can be mocked in tests (no network).
"""
from __future__ import annotations

from pathlib import Path

from .base import AdapterAvailability, SourceResult, SourceUnit

_EXTS = {".mp4", ".mov", ".m4a", ".mp3", ".wav", ".webm", ".mkv"}


class RecordingAdapter:
    name = "recording"

    def __init__(self, *, stt_key: str | None, stt_provider: str = "openai") -> None:
        self.stt_key = stt_key
        self.stt_provider = stt_provider

    def detect(self, path: Path) -> bool:
        return path.suffix.lower() in _EXTS

    def available(self) -> AdapterAvailability:
        if not self.stt_key:
            return AdapterAvailability(available=False, reason="no STT key (--stt-key)")
        return AdapterAvailability(available=True)

    def _transcribe(self, path: Path) -> list[dict[str, str]]:
        """Upload to the cloud STT provider; return [{start, text}, ...].

        Isolated for mocking. Real impl: POST the file to the provider's
        transcription endpoint with ``self.stt_key`` and parse segments.
        """
        raise NotImplementedError("real STT call wired at integration time")

    def extract(self, path: Path) -> SourceResult:
        avail = self.available()
        if not avail.available:
            return SourceResult.skip(self.name, str(path), reason=avail.reason or "unavailable")
        segments = self._transcribe(path)
        units = [
            SourceUnit(
                text=seg["text"],
                source_type=self.name,
                origin_path=str(path),
                locator=f"rec:{path.name}#{seg['start']}",
                metadata={"start": seg["start"], "provider": self.stt_provider},
            )
            for seg in segments
            if seg.get("text", "").strip()
        ]
        if not units:
            return SourceResult.skip(self.name, str(path), reason="empty transcript")
        return SourceResult.ok(self.name, str(path), units=units)
