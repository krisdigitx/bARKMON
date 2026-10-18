"""Fetch the official TensorFlow Lite YAMNet model, verifying its content."""

import hashlib
from pathlib import Path
import sys
import urllib.request

URL = "https://storage.googleapis.com/download.tensorflow.org/models/tflite/task_library/audio_classification/rpi/lite-model_yamnet_classification_tflite_1.tflite"
SHA256 = "10c95ea3eb9a7bb4cb8bddf6feb023250381008177ac162ce169694d05c317de"


def download(destination):
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("YAMNet download checksum mismatch")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)


if __name__ == "__main__":
    download(Path(sys.argv[1] if len(sys.argv) > 1 else "models/yamnet.tflite"))
