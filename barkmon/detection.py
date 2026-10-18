from dataclasses import dataclass
import zipfile

import numpy as np

RATE = 16000
HOP = 7680  # 0.48 seconds; overlapping classifier windows.


@dataclass
class Event:
    start: float
    end: float
    score: float


def merge_hits(hits, segment_start, duration):
    """Convert positive classifier hops into non-overlapping bark episodes.

    Episode count is not a count of individual vocalisations: YAMNet cannot
    resolve multiple barks in one frame. Hop durations avoid double counting
    overlapping model windows.
    """
    events = []
    for offset, score in hits:
        start = segment_start + offset
        end = segment_start + min(duration, offset + HOP / RATE)
        if end <= start:
            continue
        if events and start <= events[-1].end + 0.001:
            events[-1].end = end
            events[-1].score = max(events[-1].score, score)
        else:
            events.append(Event(start, end, score))
    return events


class Yamnet:
    def __init__(self, path, threshold):
        from tflite_runtime.interpreter import Interpreter

        self.interpreter = Interpreter(model_path=str(path), num_threads=2)
        self.interpreter.allocate_tensors()
        self.input = self.interpreter.get_input_details()[0]
        self.output = self.interpreter.get_output_details()[0]
        self.samples = int(np.prod(self.input["shape"]))
        with zipfile.ZipFile(path) as archive:
            label_file = next(n for n in archive.namelist() if n.endswith(".txt"))
            labels = archive.read(label_file).decode().splitlines()
        # Dog is too broad: panting/whimpering should not count as barking.
        self.classes = [
            i
            for i, label in enumerate(labels)
            if label.strip().lower() in {"bark", "yip", "howl", "bow-wow"}
        ]
        if not self.classes or self.output["shape"][-1] != len(labels):
            raise ValueError("Unsupported YAMNet labels or tensor shape")
        if self.input["dtype"] != np.float32:
            raise ValueError("Expected float32 YAMNet waveform model")
        self.threshold = threshold

    def detect(self, audio, start):
        hits = []
        for offset in range(0, len(audio), HOP):
            window = audio[offset : offset + self.samples]
            window = np.pad(window, (0, self.samples - len(window)))
            self.interpreter.set_tensor(
                self.input["index"],
                window.astype(np.float32).reshape(self.input["shape"]),
            )
            self.interpreter.invoke()
            scores = self.interpreter.get_tensor(self.output["index"]).reshape(-1)
            score = float(max(scores[i] for i in self.classes))
            if score >= self.threshold:
                hits.append((offset / RATE, score))
        return merge_hits(hits, start, len(audio) / RATE)
