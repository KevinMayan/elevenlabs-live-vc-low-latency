import numpy as np
import sounddevice as sd
import threading
import time
import queue
import colorama
from collections import deque

from src.settings.audio import AudioSettings


class ChunkStats:
    """Thread-safe counters for chunk filtering metrics."""

    def __init__(self):
        self._lock = threading.Lock()
        self.queued = 0
        self.dropped = 0

    def record_queued(self):
        with self._lock:
            self.queued += 1

    def record_dropped(self):
        with self._lock:
            self.dropped += 1

    def summary(self) -> str:
        with self._lock:
            total = self.queued + self.dropped
            if total == 0:
                return ""
            drop_rate = (self.dropped / total) * 100
            return (
                f"Chunks queued: {self.queued} | "
                f"Chunks dropped: {self.dropped} | "
                f"Drop rate: {drop_rate:.0f}%"
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
        self.chunk_queue = queue.Queue()
        self._chunk_counter = 0
        self._chunk_boundary_pos = 0
        self._chunk_producer_thread = None
        self.chunk_stats = ChunkStats()

    @classmethod
    def from_env(cls):
        return cls(AudioSettings.from_env())

    def get_audio_data(self) -> np.ndarray:
        return self.audio_data

    def set_vad_callback(self, callback):
        self.vad_callback = callback

    def _calculate_rms(self, audio_chunk):
        return np.sqrt(np.mean(audio_chunk ** 2))

    def chunk_contains_voice(self, chunk_data: list) -> bool:
        """Return True if the chunk has enough voiced frames to be considered speech.

        Each element of chunk_data is one sounddevice callback buffer.  We compute
        RMS per buffer and count how many exceed noise_gate_rms, then require at
        least min_speech_ratio of all frames to be voiced.
        """
        if not chunk_data:
            return False
        n_frames = len(chunk_data)
        voiced = sum(
            1 for frame in chunk_data
            if self._calculate_rms(frame) >= self.settings.noise_gate_rms
        )
        return (voiced / n_frames) >= self.settings.min_speech_ratio

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
                    self._chunk_boundary_pos = len(self.audio_data)
                    print(f"\n{colorama.Fore.GREEN}[VAD] Voice detected, recording...{colorama.Style.RESET_ALL}")
            else:
                if self.is_recording:
                    self.audio_data.append(audio_copy)

                    if rms > self.silence_threshold:
                        self.last_voice_time = time.time()
        else:
            if self.is_recording:
                self.audio_data.append(audio_copy)

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
        chunk_duration = self.settings.chunk_duration

        while not self._stop_vad:
            # Wait until actually recording (relevant for VAD mode where stream starts
            # before voice is detected)
            if not self.is_recording:
                time.sleep(0.05)
                continue

            recording_start = self.recording_start_time
            elapsed = time.time() - recording_start
            next_boundary = ((elapsed // chunk_duration) + 1) * chunk_duration
            sleep_for = next_boundary - elapsed

            time.sleep(max(0.01, sleep_for))

            if self._stop_vad or not self.is_recording:
                break

            self._extract_chunk()

    def _extract_chunk(self):
        current_pos = len(self.audio_data)
        if current_pos <= self._chunk_boundary_pos:
            return

        chunk_data = list(self.audio_data[self._chunk_boundary_pos:current_pos])
        self._chunk_boundary_pos = current_pos
        self._chunk_counter += 1
        chunk_num = self._chunk_counter
        capture_ts = time.time()

        if self.chunk_contains_voice(chunk_data):
            self.chunk_queue.put((chunk_num, chunk_data, capture_ts))
            self.chunk_stats.record_queued()
            print(
                f"{colorama.Fore.CYAN}[Chunk #{chunk_num}] queued "
                f"({len(chunk_data)} frames, speech detected){colorama.Style.RESET_ALL}"
            )
        else:
            self.chunk_stats.record_dropped()
            # Determine drop reason for the log message
            if not chunk_data:
                reason = "empty"
            else:
                n = len(chunk_data)
                voiced = sum(
                    1 for f in chunk_data
                    if self._calculate_rms(f) >= self.settings.noise_gate_rms
                )
                reason = "silence" if voiced == 0 else f"noise (ratio={voiced/n:.2f})"
            print(
                f"{colorama.Fore.YELLOW}[Chunk #{chunk_num}] dropped "
                f"({reason}){colorama.Style.RESET_ALL}"
            )

        # Print cumulative stats every 10 chunks
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
