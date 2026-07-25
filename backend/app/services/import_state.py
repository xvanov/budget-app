import time
from datetime import datetime, timezone


class _ImportState:
    def __init__(self):
        self.state = "idle"
        self.progress_pct = 0
        self.last_run = None
        self.message = ""
        self.total_files = 0
        self.processed_files = 0
        self.files = []

    def reset(self):
        self.state = "idle"
        self.progress_pct = 0
        self.message = ""
        self.total_files = 0
        self.processed_files = 0
        self.files = []

    def start(self, file_count: int):
        self.state = "importing"
        self.progress_pct = 0
        self.total_files = file_count
        self.processed_files = 0
        self.message = f"Importing {file_count} files..."

    def progress(self, processed: int, message: str = ""):
        self.processed_files = processed
        self.progress_pct = int((processed / self.total_files) * 100) if self.total_files > 0 else 0
        if message:
            self.message = message

    def finish(self):
        self.state = "complete"
        self.progress_pct = 100
        self.last_run = datetime.now(timezone.utc).isoformat()
        self.message = "Import complete"

    def fail(self, error_msg: str):
        self.state = "error"
        self.message = f"Import failed: {error_msg}"

    def to_dict(self):
        return {
            "state": self.state,
            "progress_pct": self.progress_pct,
            "last_run": self.last_run,
            "message": self.message,
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "files": self.files,
        }


import_state = _ImportState()
