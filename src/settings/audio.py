import os

class AudioSettings:
    def __init__(
        self,
        mode: int = 0,
        sample_rate=48000,
        channels=1,
        silence_threshold=0.01,
        vad_threshold=None,
        remove_background_noise=True,
        vad_silence_duration=0.8,
        vad_min_recording_duration=0.3,
        vad_pre_buffer_duration=0.5,
        input_device=None,
        chunk_duration=0.0,
        chunk_overlap=0.0,
        optimize_streaming_latency=4,
        min_speech_ratio=0.25,
        noise_gate_rms=0.01,
        use_silero_vad=False,
        silero_threshold=0.5,
    ):
        self.mode = mode
        self.sample_rate = sample_rate
        self.channels = channels
        self.silence_threshold = silence_threshold
        self.vad_threshold = silence_threshold if vad_threshold is None else vad_threshold
        self.remove_background_noise = remove_background_noise
        self.min_audio_duration = float(os.getenv("MIN_AUDIO_DURATION", 0.1))
        self.vad_silence_duration = vad_silence_duration
        self.vad_min_recording_duration = vad_min_recording_duration
        self.vad_pre_buffer_duration = vad_pre_buffer_duration
        self.input_device = input_device
        self.chunk_duration = chunk_duration
        if chunk_duration > 0 and chunk_overlap >= chunk_duration:
            clamped = chunk_duration * 0.5
            print(
                f"[Config] CHUNK_OVERLAP_SECONDS ({chunk_overlap:.2f}s) must be < "
                f"CHUNK_DURATION_SECONDS ({chunk_duration:.2f}s). Clamping to {clamped:.2f}s."
            )
            chunk_overlap = clamped
        self.chunk_overlap = chunk_overlap
        self.optimize_streaming_latency = optimize_streaming_latency
        self.min_speech_ratio = min_speech_ratio
        self.noise_gate_rms = noise_gate_rms
        self.use_silero_vad = use_silero_vad
        self.silero_threshold = silero_threshold

    def valid_modes(self):
        return [0, 1]

    @classmethod
    def from_env(cls):
        input_device = os.getenv("INPUT_DEVICE", None)
        if input_device is not None:
            try:
                input_device = int(input_device)
            except ValueError:
                input_device = None

        input_device_name = os.getenv("INPUT_DEVICE_NAME", None)
        if input_device is None and input_device_name:
            input_device = input_device_name

        return cls(
            mode=int(os.getenv("MODE", 0)),
            sample_rate=int(os.getenv("SAMPLE_RATE", 48000)),
            channels=int(os.getenv("CHANNELS", 1)),
            silence_threshold=float(os.getenv("SILENCE_THRESHOLD", 0.01)),
            vad_threshold=float(os.getenv("VAD_THRESHOLD", os.getenv("SILENCE_THRESHOLD", 0.01))),
            remove_background_noise=os.getenv("REMOVE_BACKGROUND_NOISE", "1") in ("1", "true", "True", "yes", "YES"),
            vad_silence_duration=float(os.getenv("VAD_SILENCE_DURATION", 0.8)),
            vad_min_recording_duration=float(os.getenv("VAD_MIN_RECORDING_DURATION", 0.3)),
            vad_pre_buffer_duration=float(os.getenv("VAD_PRE_BUFFER_DURATION", 0.5)),
            input_device=input_device,
            chunk_duration=float(os.getenv("CHUNK_DURATION_SECONDS", 0)),
            chunk_overlap=float(os.getenv("CHUNK_OVERLAP_SECONDS", 0)),
            optimize_streaming_latency=int(os.getenv("OPTIMIZE_STREAMING_LATENCY", 4)),
            min_speech_ratio=float(os.getenv("MIN_SPEECH_RATIO", 0.25)),
            noise_gate_rms=float(os.getenv("NOISE_GATE_RMS", 0.01)),
            use_silero_vad=os.getenv("USE_SILERO_VAD", "0") in ("1", "true", "True", "yes"),
            silero_threshold=float(os.getenv("SILERO_THRESHOLD", 0.5)),
        )
