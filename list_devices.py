import sounddevice as sd

for i, dev in enumerate(sd.query_devices()):
    print(f"[{i}] {dev['name']}")