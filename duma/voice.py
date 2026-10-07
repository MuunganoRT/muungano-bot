"""Voice notes to text, on this machine: the audio never leaves it.

Whisper runs in a child process, one note at a time. The child loads the
model, writes the text and exits, so the memory the model takes (about 800 MB
for `small`) is held only while a note is being transcribed, and the CPU work
never blocks the bot's event loop. It runs at low priority: the API shares
this machine and goes first.

The speech model is an optional install (`pip install -e '.[voice]'`); without
it Duma says it cannot listen yet. Its files (about 460 MB for `small`) are
fetched once, when Duma starts, and until they are there it says so.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

log = logging.getLogger(__name__)

LANGUAGE = "es"
SAMPLE_RATE = 16000
TIMEOUT_S = 180
DOWNLOAD = "--download"


class VoiceError(Exception):
    """A note that could not be transcribed. The message is for the log, not for the admin."""


class NotReady(VoiceError):
    """The model is still being downloaded."""


def installed() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


class Transcriber:
    def __init__(self, model: str, model_dir: Path, threads: int = 2, command: Optional[Sequence[str]] = None):
        # `command` replaces the child process in tests.
        self._command = list(command) if command else [sys.executable, "-m", "duma.voice", model, str(model_dir), str(threads)]
        self._one_at_a_time = asyncio.Lock()
        # A stand-in child has no model to fetch.
        self.ready = command is not None

    async def prepare(self) -> bool:
        """Fetch the model if it is not on disk yet. Minutes the first time, nothing afterwards.

        Kept apart from `transcribe` so a download is never cut short by the time limit of one note.
        """
        process = await asyncio.create_subprocess_exec(
            *self._command, DOWNLOAD, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, err = await process.communicate()
        self.ready = process.returncode == 0
        if not self.ready:
            reason = (err.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1]
            log.warning("could not get the speech model: %s", reason[:200])
        return self.ready

    async def transcribe(self, audio: bytes) -> str:
        if not self.ready:
            raise NotReady("the speech model is not on disk yet")
        async with self._one_at_a_time:
            process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                out, err = await asyncio.wait_for(process.communicate(audio), TIMEOUT_S)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise VoiceError(f"transcription took more than {TIMEOUT_S} s") from None
        if process.returncode != 0:
            # The last line of the child's error says what broke; the audio and its text are never logged.
            reason = (err.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1]
            raise VoiceError(f"transcriber exited with {process.returncode}: {reason[:200]}")
        return " ".join(out.decode("utf-8", "replace").split())


def _decode(data: bytes):
    """Any audio Telegram sends, as the mono 16 kHz float samples Whisper takes.

    Decoded here and not by faster-whisper: its own decoder (1.2) calls PyAV with an argument newer PyAV
    versions no longer accept.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    chunks = []
    with av.open(io.BytesIO(data)) as container:
        for frame in container.decode(audio=0):
            chunks.extend(out.to_ndarray() for out in resampler.resample(frame))
        chunks.extend(out.to_ndarray() for out in resampler.resample(None))  # what the resampler still holds
    if not chunks:
        raise ValueError("the file has no audio")
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


def _child(model_name: str, model_dir: str, threads: int, download: bool) -> None:
    os.nice(10)
    # The Xet transfer backend stalled half way through the model, with no error; plain HTTPS brought it in seconds.
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    from faster_whisper import WhisperModel, download_model

    Path(model_dir).mkdir(parents=True, exist_ok=True)
    if download:
        download_model(model_name, cache_dir=model_dir)
        return
    samples = _decode(sys.stdin.buffer.read())
    # Never fetched here: a note has a time limit and a download does not fit in it.
    model = WhisperModel(
        model_name, device="cpu", compute_type="int8", cpu_threads=threads, download_root=model_dir, local_files_only=True
    )
    segments, _ = model.transcribe(samples, language=LANGUAGE, beam_size=5)
    sys.stdout.write(" ".join(segment.text.strip() for segment in segments))


if __name__ == "__main__":
    _child(sys.argv[1], sys.argv[2], int(sys.argv[3]), download=DOWNLOAD in sys.argv[4:])
