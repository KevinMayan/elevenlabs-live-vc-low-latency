import numpy as np

_SILERO_SR = 16000
_FRAME_SIZE = 512  # 32 ms at 16 kHz — Silero VAD recommended window


class SileroVAD:
    """
    Silero VAD speech probability estimator.
    Requires: pip install silero-vad

    Audio is expected as float32 at self.sample_rate (e.g. 48 kHz).
    Internally downsamples to 16 kHz for Silero inference.
    """

    def __init__(self, sample_rate: int = 48000, threshold: float = 0.5, enabled: bool = True):
        self.sample_rate = sample_rate
        self.threshold = threshold
        self._available = False
        self._model = None
        self._torch = None
        self._downsample = max(1, sample_rate // _SILERO_SR)

        if not enabled:
            return

        try:
            import torch
            from silero_vad import load_silero_vad

            model = load_silero_vad()
            model.eval()

            self._model = model
            self._torch = torch
            self._available = True

        except ImportError as exc:
            missing = "silero-vad" if "silero" in str(exc).lower() else "torch"
            print(f"[Silero VAD] Not installed ({missing}). Run: pip install silero-vad")
        except Exception as e:
            print(f"[Silero VAD] Init error: {e}")

    @property
    def available(self) -> bool:
        return self._available

    def speech_probability(self, frame: np.ndarray) -> float:
        """
        Return speech probability (0.0–1.0) for a single audio frame.
        Uses only the first FRAME_SIZE samples at 16 kHz (32 ms).
        """
        if not self._available:
            return 0.0
        try:
            mono = frame.flatten().astype(np.float32)
            if self._downsample > 1:
                mono = mono[:: self._downsample]
            if len(mono) < _FRAME_SIZE:
                mono = np.pad(mono, (0, _FRAME_SIZE - len(mono)))
            else:
                mono = mono[:_FRAME_SIZE]
            tensor = self._torch.from_numpy(mono).unsqueeze(0)
            with self._torch.no_grad():
                prob = self._model(tensor, _SILERO_SR).item()
            return float(np.clip(prob, 0.0, 1.0))
        except Exception:
            return 0.0

    def chunk_speech_ratio(self, frames: list) -> tuple:
        """
        Analyse a full chunk of audio frames.
        Returns (speech_ratio, avg_speech_probability).

        speech_ratio  — fraction of 32 ms windows with probability >= self.threshold
        avg_prob      — mean probability across all windows

        Resets model state at the start of each call so chunks are independent.
        """
        if not self._available or not frames:
            return 0.0, 0.0

        try:
            audio = np.concatenate([f.flatten() for f in frames]).astype(np.float32)
            if self._downsample > 1:
                audio = audio[:: self._downsample]

            if len(audio) < _FRAME_SIZE:
                return 0.0, 0.0

            self._model.reset_states()
            probs = []

            for i in range(0, len(audio) - _FRAME_SIZE + 1, _FRAME_SIZE):
                window = audio[i : i + _FRAME_SIZE]
                tensor = self._torch.from_numpy(window).unsqueeze(0)
                with self._torch.no_grad():
                    prob = self._model(tensor, _SILERO_SR).item()
                probs.append(float(np.clip(prob, 0.0, 1.0)))

            if not probs:
                return 0.0, 0.0

            avg_prob = float(np.mean(probs))
            speech_ratio = sum(1 for p in probs if p >= self.threshold) / len(probs)
            return speech_ratio, avg_prob

        except Exception:
            return 0.0, 0.0
