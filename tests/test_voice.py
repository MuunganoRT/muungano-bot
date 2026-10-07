import sys

import pytest

from duma.voice import Transcriber, VoiceError


def child(code):
    """A stand-in for the Whisper process: any Python one-liner that reads the audio and prints text."""
    return [sys.executable, "-c", code]


async def test_the_audio_goes_in_and_the_text_comes_out_on_one_line(tmp_path):
    ears = Transcriber("small", tmp_path, command=child("import sys; n = len(sys.stdin.buffer.read()); print(f'  oye  Duma,\\n {n} bytes ')"))
    assert await ears.transcribe(b"x" * 1234) == "oye Duma, 1234 bytes"


async def test_a_child_that_fails_raises_with_its_last_line_and_nothing_else(tmp_path):
    ears = Transcriber("small", tmp_path, command=child("import sys; sys.stdin.buffer.read(); print('ruido', file=sys.stderr); sys.exit('model not found')"))
    with pytest.raises(VoiceError, match="exited with 1: model not found"):
        await ears.transcribe(b"audio")


async def test_a_child_that_hangs_is_killed(tmp_path, monkeypatch):
    from duma import voice

    monkeypatch.setattr(voice, "TIMEOUT_S", 0.3)
    ears = Transcriber("small", tmp_path, command=child("import time; time.sleep(30)"))
    with pytest.raises(VoiceError, match="took more than"):
        await ears.transcribe(b"audio")


def test_the_default_child_is_this_package_with_the_model_its_folder_and_threads(tmp_path):
    ears = Transcriber("small", tmp_path / "whisper", threads=2)
    assert ears._command == [sys.executable, "-m", "duma.voice", "small", str(tmp_path / "whisper"), "2"]


async def test_until_the_model_is_on_disk_a_note_is_refused_without_starting_anything(tmp_path):
    from duma.voice import NotReady

    ears = Transcriber("small", tmp_path)
    assert ears.ready is False
    with pytest.raises(NotReady):
        await ears.transcribe(b"audio")


async def test_prepare_runs_the_download_and_marks_it_ready_only_if_it_worked(tmp_path, caplog):
    ok = Transcriber("small", tmp_path, command=child("import sys; sys.exit(0 if sys.argv[-1] == '--download' else 3)"))
    ok.ready = False
    assert await ok.prepare() is True and ok.ready is True

    broken = Transcriber("small", tmp_path, command=child("import sys; sys.exit('no route to host')"))
    broken.ready = False
    with caplog.at_level("WARNING", logger="duma.voice"):
        assert await broken.prepare() is False and broken.ready is False
    assert "could not get the speech model: no route to host" in caplog.text
