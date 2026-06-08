import numpy as np
import sounddevice as sd
import threading
import time
import queue
import colorama
from collections import deque

from src.settings.audio import AudioSettings


class ChunkStats:
    """Thread-safe counters and accumulators for chunk filtering metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self.queued = 0
        self.dropped = 0
        self._total_speech_ratio = 0.0
        self._total_avg_prob = 0.0
        self._analyzed = 0

    def record_queued(self, speech_ratio: float = 0.0, avg_prob: float = 0.0):
        with self._lock:
            self.queued += 1
            self._total_speech_ratio += speech_ratio
            self._total_avg_prob += avg_prob
            self._analyzed += 1

    def record_dropped(self, speech_ratio: float = 0.0, avg_prob: float = 0.0):
        with self._lock:
            self.dropped += 1
            self._total_speech_ratio += speech_ratio
            self._total_avg_prob += avg_prob
            self._analyzed += 1

    def summary(self) -> str:
        with self._lock:
            total = self.queued + self.dropped
            if total == 0:
                return ""
            drop_rate = (self.dropped / total) * 100
            avg_ratio = self._total_speech_ratio / self._analyzed if self._analyzed else 0.0
            avg_prob = self._total_avg_prob / self._analyzed if self._analyzed else 0.0
            return (
                f"Chunks Queued: {self.queued} | "
                f"Chunks Dropped: {self.dropped} | "
                f"Drop Rate: {drop_rate:.0f}% | "
                f"Avg Speech Ratio: {avg_ratio:.2f} | "
                f"Avg Speech Probability: {avg_prob:.2f}"
            )


class AudioRecorder:
    def __init__(self, settings: AudioSettings):
        self.settings = settings
        self.is_recording = False
        self.audio_data = []
        self.stream = None

        # VAD settings
        self.vad_enabled = settings.mode == 1
        self.silence_threshold = settings.vad_threshold
        self.silence_duration = settings.vad_silence_duration
        self.min_recording_duration = settings.vad_min_recording_duration
        self.pre_buffer_duration = settings.vad_pre_buffer_duration

        # VAD state
        self.last_voice_time = 0
        self.recording_start_time = 0
        self.vad_callback = None
        self._vad_thread = None
        self._stop_vad = False
        self.voice_detected = False

        self.pre_buffer_chunks = int(self.pre_buffer_duration * settings.sample_rate / 1024) + 5
        self.pre_buffer = deque(maxlen=self.pre_buffer_chunks)

        # Chunked streaming
        self.chunked_mode = settings.chunk_duration > 0
        self._overlap_seconds = settings.chunk_overlap
        self._chunk_step = (
            max(settings.chunk_duration - settings.chunk_overlap, 0.001)
            if self.chunked_mode else settings.chunk_duration
        )
        self.chunk_queue = queue.Queue()
        self._chunk_counter = 0
        self._chunk_boundary_pos = 0
        self._audio_sample_counts = []
        self._chunk_producer_thread = None
        self.chunk_stats = ChunkStats()

        # Optional VAD processor
        self._silero = None
        self._init_processors()
        self._print_startup_info()

    def _init_processors(self):
        if self.settings.use_silero_vad:
            from src.audio.silero_vad import SileroVAD
            self._silero = SileroVAD(
                sample_rate=self.settings.sample_rate,
                threshold=self.settings.silero_threshold,
                enabled=True,
            )

    def _print_startup_info(self):
        sv_ok = self._silero is not None and self._silero.available
        sv_label = "Enabled" if sv_ok else (
            "Disabled (not installed — RMS fallback)" if self.settings.use_silero_vad
            else "Disabled (RMS fallback)"
        )

        print(
            f"{colorama.Fore.CYAN}"
            f"[Pipeline] Silero VAD: {sv_label}\n"
            f"[Pipeline] Silero Threshold: {self.settings.silero_threshold:.2f}\n"
            f"[Pipeline] Min Speech Ratio: {self.settings.min_speech_ratio:.2f}"
            f"{colorama.Style.RESET_ALL}"
        )

    @classmethod
    def from_env(cls):
        return cls(AudioSettings.from_env())

    def get_audio_data(self) -> np.ndarray:
        return self.audio_data

    def set_vad_callback(self, callback):
        self.vad_callback = callback

    def _calculate_rms(self, audio_chunk):
        return np.sqrt(np.mean(audio_chunk ** 2))

    def _analyze_chunk(self, chunk_data: list) -> tuple:
        """
        Returns (accepted, speech_ratio, avg_speech_probability).
        Uses Silero VAD when available, otherwise falls back to RMS.
        """
        if not chunk_data:
            return False, 0.0, 0.0

        silero_ok = self._silero is not None and self._silero.available
        if silero_ok:
            speech_ratio, avg_prob = self._silero.chunk_speech_ratio(chunk_data)
        else:
            n = len(chunk_data)
            voiced = sum(
                1 for f in chunk_data
                if self._calculate_rms(f) >= self.settings.noise_gate_rms
            )
            speech_ratio = voiced / n
            avg_prob = speech_ratio  # proxy when Silero is unavailable

        return speech_ratio >= self.settings.min_speech_ratio, speech_ratio, avg_prob

    def chunk_contains_voice(self, chunk_data: list) -> bool:
        """Legacy wrapper — prefer _analyze_chunk() for new code."""
        accepted, _, _ = self._analyze_chunk(chunk_data)
        return accepted

    def callback(self, indata, frames, time_info, status):
        if status:
            print(f"{colorama.Fore.YELLOW}[VAD] Input status: {status}{colorama.Style.RESET_ALL}")
        audio_copy = indata.copy()

        if self.vad_enabled:
            rms = self._calculate_rms(audio_copy)

            if not self.voice_detected:
                self.pre_buffer.append(audio_copy)

                if rms > self.silence_threshold:
                    self.voice_detected = True
                    self.is_recording = True
                    self.recording_start_time = time.time()
                    self.last_voice_time = time.time()
                    self.audio_data = list(self.pre_buffer)
                    self._audio_sample_counts = [f.shape[0] for f in self.audio_data]
                    self._chunk_boundary_pos = len(self.audio_data)
                    print(f"\n{colorama.Fore.GREEN}[VAD] Voice detected, recording...{colorama.Style.RESET_ALL}")
            else:
                if self.is_recording:
                    self.audio_data.append(audio_copy)
                    self._audio_sample_counts.append(audio_copy.shape[0])

                    if rms > self.silence_threshold:
                        self.last_voice_time = time.time()
        else:
            if self.is_recording:
                self.audio_data.append(audio_copy)
                self._audio_sample_counts.append(audio_copy.shape[0])

    def _vad_monitor(self):
        while not self._stop_vad:
            time.sleep(0.1)

            if self._stop_vad or not self.voice_detected or not self.is_recording:
                continue

            current_time = time.time()
            recording_duration = current_time - self.recording_start_time
            silence_time = current_time - self.last_voice_time

            if recording_duration > self.min_recording_duration and silence_time > self.silence_duration:
                print(f"\n{colorama.Fore.YELLOW}[VAD] Silence detected, processing...{colorama.Style.RESET_ALL}")
                self.stop()
                if self.vad_callback:
                    self.vad_callback()
                break

    def _chunk_producer(self):
        chunk_step = self._chunk_step

        while not self._stop_vad:
            if not self.is_recording:
                time.sleep(0.05)
                continue

            recording_start = self.recording_start_time
            elapsed = time.time() - recording_start
            next_boundary = ((elapsed // chunk_step) + 1) * chunk_step
            sleep_for = next_boundary - elapsed

            time.sleep(max(0.01, sleep_for))

            if self._stop_vad or not self.is_recording:
                break

            self._extract_chunk()

    def _extract_chunk(self):
        current_pos = len(self.audio_data)
        if current_pos <= self._chunk_boundary_pos:
            return

        # Build overlap window
        overlap_samples_needed = int(self._overlap_seconds * self.settings.sample_rate)
        samples_collected = 0
        overlap_start = self._chunk_boundary_pos
        while overlap_start > 0 and samples_collected < overlap_samples_needed:
            overlap_start -= 1
            samples_collected += self._audio_sample_counts[overlap_start]

        old_boundary = self._chunk_boundary_pos
        chunk_data = list(self.audio_data[overlap_start:current_pos])
        overlap_frame_count = old_boundary - overlap_start

        self._chunk_counter += 1
        chunk_num = self._chunk_counter
        capture_ts = time.time()

        chunk_total_samples = sum(f.shape[0] for f in chunk_data)
        chunk_duration_s = chunk_total_samples / self.settings.sample_rate
        window_end_s = capture_ts - self.recording_start_time
        window_start_s = window_end_s - chunk_duration_s
        actual_overlap_s = samples_collected / self.settings.sample_rate

        # Advance boundary
        self._chunk_boundary_pos = current_pos

        # Trim audio_data front — keep only the overlap window for the next chunk
        if overlap_start > 0:
            del self.audio_data[:overlap_start]
            del self._audio_sample_counts[:overlap_start]
            self._chunk_boundary_pos -= overlap_start

        # --- Silero VAD / RMS chunk analysis ---
        t_vad = time.perf_counter()
        accepted, speech_ratio, avg_prob = self._analyze_chunk(chunk_data)
        vad_ms = (time.perf_counter() - t_vad) * 1000

        perf_line = f"  Silero: {vad_ms:.0f}ms | Validation: {vad_ms:.0f}ms"

        if accepted:
            self.chunk_queue.put((chunk_num, chunk_data, capture_ts, overlap_frame_count))
            self.chunk_stats.record_queued(speech_ratio, avg_prob)
            print(
                f"{colorama.Fore.CYAN}"
                f"[Chunk #{chunk_num}]\n"
                f"  Speech Ratio: {speech_ratio:.2f}\n"
                f"  Average Speech Probability: {avg_prob:.2f}\n"
                f"  Result: QUEUED\n"
                f"  Duration: {chunk_duration_s:.1f}s | Overlap: {actual_overlap_s:.1f}s | "
                f"Window: {window_start_s:.1f}s → {window_end_s:.1f}s\n"
                f"{perf_line}"
                f"{colorama.Style.RESET_ALL}"
            )
        else:
            self.chunk_stats.record_dropped(speech_ratio, avg_prob)
            print(
                f"{colorama.Fore.YELLOW}"
                f"[Chunk #{chunk_num}]\n"
                f"  Speech Ratio: {speech_ratio:.2f}\n"
                f"  Average Speech Probability: {avg_prob:.2f}\n"
                f"  Result: DROPPED\n"
                f"{perf_line}"
                f"{colorama.Style.RESET_ALL}"
            )

        if self._chunk_counter % 10 == 0:
            summary = self.chunk_stats.summary()
            if summary:
                print(f"{colorama.Fore.MAGENTA}[Stats] {summary}{colorama.Style.RESET_ALL}")

    def _flush_final_chunk(self):
        self._extract_chunk()

    def start(self):
        if not self.is_recording:
            self.is_recording = True
            self.audio_data = []
            self._audio_sample_counts = []
            self.recording_start_time = time.time()
            self.last_voice_time = time.time()
            self._stop_vad = False
            self.voice_detected = True
            self._chunk_boundary_pos = 0

            self.stream = sd.InputStream(
                callback=self.callback,
                channels=self.settings.channels,
                samplerate=self.settings.sample_rate,
                dtype='float32',
                device=self.settings.input_device
            )
            self.stream.start()

            if self.vad_enabled:
                self._vad_thread = threading.Thread(target=self._vad_monitor, daemon=True)
                self._vad_thread.start()

            if self.chunked_mode:
                self._chunk_producer_thread = threading.Thread(
                    target=self._chunk_producer, daemon=True
                )
                self._chunk_producer_thread.start()

    def stop(self):
        if self.is_recording and self.stream is not None:
            self._stop_vad = True
            self.stream.stop()
            self.stream.close()
            self.is_recording = False
            self.voice_detected = False

            if self.chunked_mode:
                self._flush_final_chunk()

    def start_continuous(self):
        if self.vad_enabled:
            self.audio_data = []
            self._audio_sample_counts = []
            self.pre_buffer.clear()
            self.voice_detected = False
            self.is_recording = False
            self._stop_vad = False
            self._chunk_boundary_pos = 0

            print(f"{colorama.Fore.GREEN}[VAD] Listening for voice...{colorama.Style.RESET_ALL}")

            self.stream = sd.InputStream(
                callback=self.callback,
                channels=self.settings.channels,
                samplerate=self.settings.sample_rate,
                dtype='float32',
                device=self.settings.input_device
            )
            self.stream.start()

            self._vad_thread = threading.Thread(target=self._vad_monitor, daemon=True)
            self._vad_thread.start()

            if self.chunked_mode:
                self._chunk_producer_thread = threading.Thread(
                    target=self._chunk_producer, daemon=True
                )
                self._chunk_producer_thread.start()
