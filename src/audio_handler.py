import queue
import threading
import time
import colorama
import keyboard
from src.audio_processor import AudioProcessor
from src.audio_recorder import AudioRecorder
from src.el_client import ElevenLabsClient


class AudioHandler:
    def __init__(self, recorder: AudioRecorder, processor: AudioProcessor, el_client: ElevenLabsClient):
        self.recorder = recorder
        self.processor = processor
        self.el_client = el_client
        self.chunked_mode = recorder.chunked_mode

        keyboard.on_press_key("space", self.handle_recording)

        if self.recorder.vad_enabled:
            self.recorder.set_vad_callback(self.process_vad_recording)

        self._upload_worker_thread: threading.Thread | None = None
        self._upload_worker_running = False

        if self.chunked_mode:
            self._start_upload_worker()

    @classmethod
    def from_env(cls):
        return cls(
            AudioRecorder.from_env(),
            AudioProcessor.from_env(),
            ElevenLabsClient.from_env()
        )

    def _start_upload_worker(self):
        self._upload_worker_running = True
        self._upload_worker_thread = threading.Thread(
            target=self._upload_worker, daemon=True, name="upload-worker"
        )
        self._upload_worker_thread.start()

        chunk_duration = self.recorder.settings.chunk_duration
        chunk_overlap = self.recorder.settings.chunk_overlap
        chunk_step = self.recorder._chunk_step
        print(
            f"{colorama.Fore.GREEN}[Upload] Worker started (chunked mode)\n"
            f"  Chunk Duration: {chunk_duration:.1f}s\n"
            f"  Chunk Overlap:  {chunk_overlap:.1f}s\n"
            f"  Effective Step: {chunk_step:.1f}s"
            f"{colorama.Style.RESET_ALL}"
        )

    def _upload_worker(self):
        while self._upload_worker_running:
            try:
                chunk_num, chunk_data, capture_ts, overlap_frame_count = self.recorder.chunk_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            upload_start = time.time()
            print(
                f"{colorama.Fore.CYAN}[Chunk #{chunk_num}] uploading...{colorama.Style.RESET_ALL}"
            )

            audio_stream = self.processor.get_audio_stream(chunk_data)
            if audio_stream is None:
                print(
                    f"{colorama.Fore.YELLOW}[Chunk #{chunk_num}] skipped "
                    f"(no usable audio){colorama.Style.RESET_ALL}"
                )
                continue

            self.el_client.convert_audio(
                audio_stream,
                remove_background_noise=self.recorder.settings.remove_background_noise,
                chunk_num=chunk_num,
                capture_ts=capture_ts,
                upload_start=upload_start,
                overlap_frame_count=overlap_frame_count,
            )

    def stop_upload_worker(self):
        self._upload_worker_running = False

    def handle_recording(self, event):
        if self.recorder.is_recording:
            print(
                f"\n{colorama.Fore.GREEN}Recording stopped, processing audio...{
                    colorama.Style.RESET_ALL}"
            )
            self.recorder.stop()

            if not self.chunked_mode:
                audio = self.recorder.get_audio_data()
                self.el_client.convert_audio(
                    self.processor.get_audio_stream(audio),
                    remove_background_noise=self.recorder.settings.remove_background_noise,
                )

            if self.recorder.vad_enabled:
                self.recorder.start_continuous()
        else:
            print(
                f"\n{colorama.Fore.GREEN}Start recording, press space to stop...{
                    colorama.Style.RESET_ALL}"
            )
            self.recorder.start()

    def process_vad_recording(self):
        if not self.chunked_mode:
            audio = self.recorder.get_audio_data()
            audio_stream = self.processor.get_audio_stream(audio)
            if audio_stream is None:
                print(
                    f"{colorama.Fore.YELLOW}No usable audio recorded. "
                    f"Try speaking longer.{colorama.Style.RESET_ALL}"
                )
                self.recorder.start_continuous()
                return
            self.el_client.convert_audio(
                audio_stream,
                remove_background_noise=self.recorder.settings.remove_background_noise,
            )

        self.recorder.start_continuous()

    def start_vad_mode(self):
        print(f"{colorama.Fore.CYAN}=== VAD Mode Active ==={colorama.Style.RESET_ALL}")
        print(f"{colorama.Fore.CYAN}Speak naturally - recording starts/stops automatically{colorama.Style.RESET_ALL}")
        print(f"{colorama.Fore.CYAN}Press SPACE to manually trigger, Ctrl+C to exit{colorama.Style.RESET_ALL}")
        self.recorder.start_continuous()
