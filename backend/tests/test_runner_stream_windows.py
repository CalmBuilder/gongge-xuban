import subprocess
import sys

from app.general_skills import runner


def test_stream_output_reads_stdout_cross_platform() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('hello-out'); sys.stderr.write('hello-err')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err, timed_out = runner._stream_process_output(proc, [], None, 1)
    assert "hello-out" in out
    assert "hello-err" in err
    assert timed_out is False


def test_threaded_reader_reads_output_directly() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdout.write('t-out'); sys.stderr.write('t-err')"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err, timed_out = runner._stream_process_output_threaded(proc, [], None, 1)
    assert "t-out" in out
    assert "t-err" in err
    assert timed_out is False


def test_windows_stream_impl_selected(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    assert runner._use_thread_reader() is True


def test_posix_stream_impl_selected(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert runner._use_thread_reader() is False
