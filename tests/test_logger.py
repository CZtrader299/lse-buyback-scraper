"""Tests for file logging."""
import os, sys, pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from logger import TeeLogger


class TestTeeLogger:
    def test_creates_log_file(self, tmp_path):
        log_path = tmp_path / "test.log"
        tee = TeeLogger(str(log_path))
        tee.write("hello\n")
        tee.flush()
        tee.close()
        assert log_path.exists()
        assert "hello" in log_path.read_text()

    def test_captures_print_output(self, tmp_path):
        log_path = tmp_path / "test.log"
        tee = TeeLogger(str(log_path))
        old_stdout = sys.stdout
        sys.stdout = tee
        try:
            print("test message")
            tee.flush()
        finally:
            sys.stdout = old_stdout
        tee.close()
        content = log_path.read_text()
        assert "test message" in content

    def test_encoding_attribute(self, tmp_path):
        log_path = tmp_path / "test.log"
        tee = TeeLogger(str(log_path))
        assert tee.encoding == 'utf-8'
        tee.close()

    def test_handles_unicode(self, tmp_path):
        log_path = tmp_path / "test.log"
        tee = TeeLogger(str(log_path))
        tee.write("✓ £42.50 — HANA\n")
        tee.flush()
        tee.close()
        content = log_path.read_text(encoding='utf-8')
        assert "✓ £42.50" in content

    def test_close_is_idempotent(self, tmp_path):
        log_path = tmp_path / "test.log"
        tee = TeeLogger(str(log_path))
        tee.close()
        tee.close()  # should not raise
