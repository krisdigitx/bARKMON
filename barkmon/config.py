import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("DATA_DIR", "/data"))
    model_path: Path = Path(os.getenv("MODEL_PATH", "/models/yamnet.tflite"))
    threshold: float = float(os.getenv("BARK_THRESHOLD", "0.35"))
    retention_days: int = int(os.getenv("RETENTION_DAYS", "7"))
    max_storage_bytes: int = int(float(os.getenv("MAX_STORAGE_GIB", "16")) * 1024**3)
    min_free_bytes: int = int(float(os.getenv("MIN_FREE_GIB", "1")) * 1024**3)
    timezone: str = os.getenv("REPORT_TIMEZONE", "Europe/London")
    night_start: int = int(os.getenv("NIGHT_START_HOUR", "22"))
    night_end: int = int(os.getenv("NIGHT_END_HOUR", "7"))
    segment_seconds: int = 60

    def __post_init__(self):
        if not 0 < self.threshold <= 1:
            raise ValueError("BARK_THRESHOLD must be in (0, 1]")
        if (
            self.retention_days < 1
            or self.max_storage_bytes <= 0
            or self.min_free_bytes < 0
        ):
            raise ValueError("Invalid retention or storage configuration")
        if not 0 <= self.night_start < 24 or not 0 <= self.night_end < 24:
            raise ValueError("Night hours must be between 0 and 23")

    @property
    def recordings(self):
        return self.data_dir / "recordings"
