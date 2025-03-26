import os

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = r"stttesting-445210-aa5e435ad2b1.json"

import html
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import colorchooser
from google.cloud import speech, translate
import sounddevice as sd
import inspect
import numpy as np

# Audio recording parameters
RATE = 16000
CHUNK = RATE // 20  # 50ms chunks

# Global settings for language, subtitle color, and device selection
selected_translation = "fr-en"  # Default: French ➝ English
subtitle_color = "white"  # Default subtitle color
selected_device = None  # Default device (None implies system default)
exit_flag = False  # Flag to signal exit


class SystemAudioStream:
    """Captures system (output) audio using sounddevice with WASAPI loopback enabled and yields audio chunks.
       Opens the stream using the device’s native channel count and then downmixes to mono."""

    def __init__(self, rate: int = RATE, chunk: int = CHUNK, device=None) -> None:
        self.rate = rate
        self.chunk = chunk
        self.device = device
        self.q = queue.Queue()
        self.stream = None
        self.channels = 1  # will be updated

    def __enter__(self):
        stream_kwargs = {}
        try:
            # Check if WASAPI supports the "loopback" keyword
            params = inspect.signature(sd.WasapiSettings).parameters
            if "loopback" in params:
                wasapi_settings = sd.WasapiSettings(loopback=True)
            else:
                wasapi_settings = sd.WasapiSettings()
            stream_kwargs = {"extra_settings": wasapi_settings}
        except Exception as e:
            print("Warning: Could not create WASAPI settings with loopback. Error:", e)
            stream_kwargs = {}

        # Determine native channel count from the device info.
        try:
            if self.device is not None:
                device_info = sd.query_devices(self.device)
            else:
                # Query default output device info
                device_info = sd.query_devices(None, 'output')
            self.channels = device_info['max_output_channels']
        except Exception as e:
            print("Warning: Could not query device info, defaulting channels to 1. Error:", e)
            self.channels = 1

        self.stream = sd.RawInputStream(
            samplerate=self.rate,
            blocksize=self.chunk,
            dtype='int16',
            channels=self.channels,  # use native output channels
            device=self.device,
            callback=self.callback,
            **stream_kwargs
        )
        self.stream.start()
        return self

    def callback(self, indata, frames, time, status):
        if status:
            print(status, file=sys.stderr)
        # Downmix to mono if more than one channel by taking the first channel.
        audio_array = np.frombuffer(indata, dtype=np.int16)
        if self.channels > 1:
            audio_array = audio_array.reshape(-1, self.channels)
            audio_array = audio_array[:, 0]  # take first channel
        self.q.put(audio_array.tobytes())

    def __exit__(self, exc_type, exc_value, traceback):
        if self.stream:
            self.stream.stop()
            self.stream.close()

    def generator(self):
        while True:
            data = self.q.get()
            if data is None:
                break
            yield data


def translate_text(texts: list[str], target_language: str) -> list[str]:
    """Batch translation to reduce API calls & latency."""
    client = translate.TranslationServiceClient()
    project_id = "stttesting-445210"
    parent = f"projects/{project_id}/locations/global"

    response = client.translate_text(
        request={
            "contents": texts,
            "target_language_code": target_language,
            "parent": parent,
        }
    )
    return [html.unescape(translation.translated_text) for translation in response.translations]


def listen_print_loop(responses, transcript_queue):
    global exit_flag
    num_chars_printed = 0

    # Set the language direction based on user selection
    source_language, target_language = selected_translation.split("-")

    for response in responses:
        if not response.results:
            continue

        result = response.results[0]
        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript
        overwrite_chars = " " * (num_chars_printed - len(transcript))

        if not result.is_final:
            transcript_queue.put(transcript + overwrite_chars)
            num_chars_printed = len(transcript)
        else:
            if not transcript.strip():
                num_chars_printed = 0
                continue

            translations = translate_text([transcript], target_language=target_language)
            translated_text = translations[0] if translations else transcript
            transcript_queue.put(translated_text)

            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                transcript_queue.put("Exiting..")
                exit_flag = True
                break

            num_chars_printed = 0

    if exit_flag:
        transcript_queue.put(None)


def run_transcription(transcript_queue):
    """Runs speech recognition on system audio and passes results to the queue."""
    global exit_flag, selected_device
    client = speech.SpeechClient()

    source_language, _ = selected_translation.split("-")
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code=source_language,
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True, single_utterance=False
    )

    with SystemAudioStream(RATE, CHUNK, device=selected_device) as stream:
        audio_generator = stream.generator()
        requests = (
            speech.StreamingRecognizeRequest(audio_content=content)
            for content in audio_generator
        )
        responses = client.streaming_recognize(streaming_config, requests)
        listen_print_loop(responses, transcript_queue)


def settings_window(root):
    """Settings window for selecting translation mode, subtitle color, and output audio device."""
    global selected_translation, subtitle_color, selected_device

    def apply_settings():
        global selected_translation, subtitle_color, selected_device
        selected_translation = lang_var.get()
        subtitle_color = color_var.get()

        choice = device_var.get()
        if choice == "Default":
            selected_device = None
        else:
            selected_device = int(choice.split(":")[0])
        settings.destroy()

    def choose_color():
        color_code = colorchooser.askcolor(title="Choose Text Color")[1]
        if color_code:
            color_var.set(color_code)

    settings = tk.Toplevel(root)
    settings.title("Settings")
    settings.geometry("400x300")

    # Translation mode selection
    tk.Label(settings, text="Select Translation Mode:").pack(pady=5)
    lang_var = tk.StringVar(value=selected_translation)
    tk.Radiobutton(settings, text="French ➝ English", variable=lang_var, value="fr-en").pack()
    tk.Radiobutton(settings, text="English ➝ French", variable=lang_var, value="en-fr").pack()

    # Subtitle color selection
    tk.Label(settings, text="Choose Subtitle Color:").pack(pady=5)
    color_var = tk.StringVar(value=subtitle_color)
    tk.Button(settings, text="Pick a Color", command=choose_color).pack(pady=5)

    # Output audio device selection (WASAPI devices only)
    tk.Label(settings, text="Select Output Audio Device:").pack(pady=5)
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        # Find WASAPI host API index
        wasapi_index = None
        for i, host in enumerate(hostapis):
            if "wasapi" in host['name'].lower():
                wasapi_index = i
                break
        if wasapi_index is not None:
            output_devices = [(i, dev['name']) for i, dev in enumerate(devices)
                              if dev['max_output_channels'] > 0 and dev['hostapi'] == wasapi_index]
        else:
            output_devices = [(i, dev['name']) for i, dev in enumerate(devices) if dev['max_output_channels'] > 0]
    except Exception as e:
        output_devices = []
        print("Error querying devices:", e)
    options = ["Default"] + [f"{i}: {name}" for i, name in output_devices]
    device_var = tk.StringVar(value="Default")
    tk.OptionMenu(settings, device_var, *options).pack(pady=5)

    tk.Button(settings, text="Apply", command=apply_settings).pack(pady=10)
    settings.grab_set()
    return settings


def main():
    """Main application logic."""
    root = tk.Tk()
    root.withdraw()  # Hide main window until settings are selected

    settings = settings_window(root)
    root.wait_window(settings)  # Wait for settings window to be closed

    root.deiconify()  # Show the main window

    transcript_queue = queue.Queue()
    transcription_thread = threading.Thread(target=run_transcription, args=(transcript_queue,))
    transcription_thread.daemon = True
    transcription_thread.start()

    # Create a borderless overlay window for subtitles.
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.config(bg="gray")
    try:
        root.wm_attributes("-transparentcolor", "black")
    except tk.TclError:
        pass

    screen_width = root.winfo_screenwidth()
    screen_height = root.winfo_screenheight()
    window_height = 100
    x_pos = 0
    y_pos = screen_height - window_height - 50
    root.geometry(f"{screen_width}x{window_height}+{x_pos}+{y_pos}")

    subtitle_label = tk.Label(
        root,
        text="",
        font=("Helvetica", 28),
        fg=subtitle_color,
        bg="black",
        wraplength=screen_width - 100,
        justify="center"
    )
    subtitle_label.pack(expand=True, fill="both")

    def poll_queue():
        """Update subtitles in real time from the transcript queue."""
        try:
            while True:
                message = transcript_queue.get_nowait()
                if message is None:
                    root.quit()
                    return
                subtitle_label.config(text=message, fg=subtitle_color)
        except queue.Empty:
            pass
        root.after(25, poll_queue)

    poll_queue()
    root.mainloop()
    transcription_thread.join()


if __name__ == "__main__":
    main()
