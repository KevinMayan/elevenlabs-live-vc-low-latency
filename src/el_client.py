from io import BytesIO
import os
import time
import threading
import numpy as np
import sounddevice as sd
import colorama
from scipy.signal import resample_poly
from elevenlabs.client import ElevenLabs


class LatencyStats:
    def __init__(self):
        self._lock = threading.Lock()
        self.count = 0
        self._total_first_chunk_ms = 0.0
        self._total_upload_delay_ms = 0.0

    def record(self, first_chunk_ms: float, upload_delay_ms: float):
        with self._lock:
            self.count += 1
            self._total_first_chunk_ms += first_chunk_ms
            self._total_upload_delay_ms += upload_delay_ms

    def summary(self) -> str:
        with self._lock:
            if self.count == 0:
                return ""
            avg_fc = self._total_first_chunk_ms / self.count
            avg_ud = self._total_upload_delay_ms / self.count
            return (
                f"Rolling avg (n={self.count}) | "
                f"First-chunk: {avg_fc:.0f}ms | "
                f"Queue delay: {avg_ud:.0f}ms"
            )


class ElevenLabsClient:
    def __init__(
        self,
        api_key,
        voice_id,
        output_device=None,
        output_sample_rate=48000,
        api_sample_rate=22050,
        optimize_streaming_latency=4,
    ):
        self.client = ElevenLabs(api_key=api_key)
        self.voice_id = voice_id
        self.output_device = output_device
        self.output_sample_rate = output_sample_rate
        self.api_sample_rate = api_sample_rate
        self.optimize_streaming_latency = optimize_streaming_latency

        self._stream: sd.OutputStream | None = None
        self._stream_lock = threading.Lock()
        self.latency_stats = LatencyStats()

        print(
            f"{colorama.Fore.CYAN}[EL] optimize_streaming_latency={optimize_streaming_latency}"
            f"{colorama.Style.RESET_ALL}"
        )

    @classmethod
    def from_env(cls):
        output_device = os.getenv("OUTPUT_DEVICE", None)
        if output_device is not None:
            try:
                output_device = int(output_device)
            except ValueError:
                output_device = None

        output_device_name = os.getenv("OUTPUT_DEVICE_NAME", None)
        if output_device is None and output_device_name:
            name_lower = output_device_name.lower()
            for i, device in enumerate(sd.query_devices()):
                if name_lower in device["name"].lower() and device["max_output_channels"] > 0:
                    output_device = i
                    break

        return cls(
            os.getenv("API_KEY", None),
            os.getenv("VOICE_ID", None),
            output_device=output_device,
            output_sample_rate=int(os.getenv("OUTPUT_SAMPLE_RATE", 48000)),
            api_sample_rate=int(os.getenv("API_SAMPLE_RATE", 22050)),
            optimize_streaming_latency=int(os.getenv("OPTIMIZE_STREAMING_LATENCY", 4)),
        )

    def _resample(self, samples: np.ndarray, orig_rate: int, target_rate: int) -> np.ndarray:
        if orig_rate == target_rate:
            return samples
        if samples.size == 0:
            return samples
        return resample_poly(samples, target_rate, orig_rate).astype(np.float32)

    def _ensure_stream(self):
        if self._stream is None:
            self._stream = sd.OutputStream(
                samplerate=self.output_sample_rate,
                channels=1,
                dtype="float32",
                device=self.output_device,
            )
            self._stream.start()
            print(f"{colorama.Fore.GREEN}[Playback] Persistent output stream opened{colorama.Style.RESET_ALL}")

    def close(self):
        with self._stream_lock:
            if self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None

    def convert_audio(
        self,
        audio: BytesIO,
        remove_background_noise: bool,
        chunk_num: int | None = None,
        capture_ts: float | None = None,
        upload_start: float | None = None,
        overlap_frame_count: int = 0,
    ):
        label = f"[Chunk #{chunk_num}]" if chunk_num is not None else "[Audio]"
        request_start = time.time()

        try:
            audio_stream = self.client.speech_to_speech.convert_as_stream(
                voice_id=self.voice_id,
                audio=audio,
                output_format="pcm_22050",
                remove_background_noise=remove_background_noise,
                optimize_streaming_latency=self.optimize_streaming_latency,
            )
        except TypeError:
            # SDK version doesn't support optimize_streaming_latency
            audio_stream = self.client.speech_to_speech.convert_as_stream(
                voice_id=self.voice_id,
                audio=audio,
                output_format="pcm_22050",
                remove_background_noise=remove_background_noise,
            )

        with self._stream_lock:
            self._ensure_stream()

        first_chunk_time = None
        for chunk in audio_stream:
            if not chunk:
                continue

            if first_chunk_time is None:
                first_chunk_time = time.time()
                first_chunk_ms = (first_chunk_time - request_start) * 1000
                upload_delay_ms = (upload_start - capture_ts) * 1000 if capture_ts and upload_start else 0.0
                playback_start_ms = (first_chunk_time - (capture_ts or request_start)) * 1000

                if chunk_num is not None:
                    print(
                        f"{colorama.Fore.CYAN}{label} "
                        f"Capture→First-audio: {playback_start_ms:.0f}ms | "
                        f"Upload delay: {upload_delay_ms:.0f}ms | "
                        f"EL latency: {first_chunk_ms:.0f}ms"
                        f"{colorama.Style.RESET_ALL}"
                    )
                    self.latency_stats.record(first_chunk_ms, upload_delay_ms)
                    summary = self.latency_stats.summary()
                    if summary:
                        print(f"{colorama.Fore.CYAN}  {summary}{colorama.Style.RESET_ALL}")
                else:
                    print(f"{colorama.Fore.CYAN}{label} First chunk in {first_chunk_ms:.0f}ms{colorama.Style.RESET_ALL}")

            samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
            if samples.size == 0:
                continue
            samples = self._resample(samples, self.api_sample_rate, self.output_sample_rate)

            with self._stream_lock:
                self._stream.write(samples.reshape(-1, 1))
