"""Tee logger — duplicates stdout/stderr to a timestamped log file.

Usage:
    tee_out = TeeLogger("path/to/logfile.log")
    tee_err = TeeLogger("path/to/logfile.log", stream=sys.__stderr__)
    sys.stdout = tee_out
    sys.stderr = tee_err
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    tee_out.close()
    tee_err.close()
"""

import sys


class TeeLogger:
    """Write to both a file and the original stream (stdout or stderr)."""

    def __init__(self, log_path, stream=None):
        self._stream = stream or sys.__stdout__
        self._file = open(log_path, 'a', encoding='utf-8', errors='replace')
        self.encoding = 'utf-8'

    def write(self, message):
        try:
            self._stream.write(message)
        except (UnicodeEncodeError, OSError):
            pass
        try:
            self._file.write(message)
        except (UnicodeEncodeError, OSError):
            pass

    def flush(self):
        try:
            self._stream.flush()
        except OSError:
            pass
        try:
            self._file.flush()
        except OSError:
            pass

    def close(self):
        if self._file and not self._file.closed:
            self._file.close()

    def reconfigure(self, **kwargs):
        """No-op — compatibility with sys.stdout.reconfigure()."""
        pass

    def fileno(self):
        return self._stream.fileno()

    @property
    def errors(self):
        return 'replace'

    def isatty(self):
        return False
