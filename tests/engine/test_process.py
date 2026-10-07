import os
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from engine.funsearch._process import LOG_BYTES, _StderrPrefix, run_command


class CommandOutputTests(unittest.TestCase):
    def test_large_stderr_is_drained_into_bounded_memory_without_disk_spooling(self):
        script = "import sys; sys.stderr.buffer.write(b'x' * (16 * 1024 * 1024))"
        readers = []

        def capture(pipe):
            reader = _StderrPrefix(pipe)
            readers.append(reader)
            return reader

        with tempfile.TemporaryDirectory() as directory, \
                patch("engine.funsearch._process._StderrPrefix", side_effect=capture), \
                patch("engine.funsearch._process.subprocess.Popen", wraps=subprocess.Popen) as spawn:
            ok, log = run_command(f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                                  directory, 5)
        self.assertTrue(ok)
        self.assertEqual(log, "x" * LOG_BYTES)
        self.assertEqual(spawn.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertEqual(len(readers[0].buffer), LOG_BYTES)
        self.assertFalse(readers[0].thread.is_alive())
        self.assertTrue(readers[0].pipe.closed)

    def test_reader_stops_when_descendant_keeps_stderr_open(self):
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        reader = _StderrPrefix(os.fdopen(read_fd, "rb", buffering=0))
        self.addCleanup(reader.close)
        os.write(write_fd, b"diagnostic")
        started = time.monotonic()
        self.assertEqual(reader.close(), "diagnostic")
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(reader.thread.is_alive())
