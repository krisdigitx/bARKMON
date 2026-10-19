import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


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
    camera_id: str = "camera1"
    camera_name: str = "Camera 1"
    camera_host: str = os.getenv("CAMERA_HOST", "192.168.1.207")
    camera_path: str = os.getenv("CAMERA_PATH", "/stream1")
    camera2_host: str = os.getenv("CAMERA2_HOST", "")
    camera2_path: str = os.getenv("CAMERA2_PATH", "/stream1")
    dedup_seconds: float = float(os.getenv("DEDUP_SECONDS", "0.75"))
    dedup_similarity: float = float(os.getenv("DEDUP_SIMILARITY", "0.85"))
    episode_gap_seconds: float = float(os.getenv("EPISODE_GAP_SECONDS", "1"))
    report_retention_days: int = int(os.getenv("REPORT_RETENTION_DAYS", "90"))
    report_max_bytes: int = int(float(os.getenv("REPORT_MAX_MIB", "256")) * 1024**2)
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "")
    report_base_url: str = os.getenv("REPORT_BASE_URL", "")
    segment_seconds: int = 60

    def __post_init__(self):
        if self.public_base_url:
            url = urlsplit(self.public_base_url)
            if (
                url.scheme not in ("http", "https")
                or not url.netloc
                or url.username
                or url.password
                or url.path not in ("", "/")
                or url.query
                or url.fragment
            ):
                raise ValueError("PUBLIC_BASE_URL must be an HTTP(S) origin")
        if (
            not 0 <= self.dedup_seconds <= 5
            or not 0 <= self.dedup_similarity <= 1
            or not 0 <= self.episode_gap_seconds <= 10
        ):
            raise ValueError("Invalid timeline merging settings")
        if self.report_retention_days < 1 or self.report_max_bytes <= 0:
            raise ValueError("Invalid report retention settings")
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


def camera_settings(settings):
    """Preserve the first camera's existing files; isolate each other camera."""
    from dataclasses import replace

    cameras = [settings]
    if settings.camera2_host:
        cameras.append(
            replace(
                settings,
                camera_id="camera2",
                camera_name="Camera 2",
                camera_host=settings.camera2_host,
                camera_path=settings.camera2_path,
                data_dir=settings.data_dir / "cameras" / "camera2",
            )
        )
    return [
        replace(camera, max_storage_bytes=settings.max_storage_bytes // len(cameras))
        for camera in cameras
    ]
