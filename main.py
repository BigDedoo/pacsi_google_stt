import sys
import os
import time  # Added to allow a delay before restarting the audio stream
import keyboard  # For global hotkeys

# If running as a bundled executable, set the credentials path to the extracted file.
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(".")

credentials_path = os.path.join(base_path, "stttesting-445210-aa5e435ad2b1.json")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

import html
import queue
import re
import threading
import tkinter as tk
from tkinter import colorchooser

from google.cloud import speech, translate
import pyaudio

# Audio recording parameters
RATE = 16000
CHUNK = RATE // 20  # 50ms updates

# Global control event and transcription thread holder.
stop_event = threading.Event()
transcription_thread = None
overlay = None  # Global variable to hold the overlay window


class MicrophoneStream:
    """Opens a recording stream as a generator yielding audio chunks."""
    def __init__(self, rate: int = RATE, chunk: int = CHUNK, input_device_index: int = None) -> None:
        self._rate = rate
        self._chunk = chunk
        self.input_device_index = input_device_index
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self) -> "MicrophoneStream":
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._rate,
            input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self._chunk,
            stream_callback=self._fill_buffer,
        )
        self.closed = False
        return self

    def __exit__(self, type, value, traceback) -> None:
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        # Unblock any pending queue operations.
        self._buff.put(None)
        self._audio_interface.terminate()
        del self._audio_interface  # Ensure complete cleanup

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        # Use a timeout so that we periodically check the stop_event.
        while not self.closed:
            try:
                chunk = self._buff.get(timeout=0.5)
            except queue.Empty:
                if stop_event.is_set():
                    return
                continue
            if chunk is None:
                return
            data = [chunk]
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    data.append(chunk)
                except queue.Empty:
                    break
            yield b"".join(data)


def translate_text(texts: list[str], target_language: str = "en") -> list[str]:
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


def listen_print_loop(responses, transcript_queue, target_language_code):
    num_chars_printed = 0
    exit_flag = False

    for response in responses:
        if stop_event.is_set():
            break
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
            translations = translate_text([transcript], target_language=target_language_code)
            translated_text = translations[0] if translations else transcript
            transcript_queue.put(translated_text)

            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                transcript_queue.put("Exiting..")
                exit_flag = True
                break

            num_chars_printed = 0

    transcript_queue.put(None)
    return exit_flag


def run_transcription(transcript_queue, source_language_code, target_language_code, input_device_index):
    # Determine sample rate from the selected device, or use the default RATE.
    pya_instance = pyaudio.PyAudio()
    if input_device_index is not None:
        device_info = pya_instance.get_device_info_by_index(input_device_index)
        sample_rate = int(device_info["defaultSampleRate"])
    else:
        sample_rate = RATE
    pya_instance.terminate()

    chunk = sample_rate // 20  # 50ms of audio

    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=sample_rate,
        language_code=source_language_code,
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True, single_utterance=False
    )

    # Continuously restart the stream to avoid exceeding the 305-second limit.
    while True:
        if stop_event.is_set():
            break
        print("Restarting stream...")

        try:
            try:
                with MicrophoneStream(rate=sample_rate, chunk=chunk, input_device_index=input_device_index) as stream:
                    audio_generator = stream.generator()
                    requests = (speech.StreamingRecognizeRequest(audio_content=content)
                                for content in audio_generator)
                    should_exit = listen_print_loop(
                        client.streaming_recognize(streaming_config, requests),
                        transcript_queue,
                        target_language_code
                    )
                    if should_exit:
                        break
            except OSError as audio_error:
                print(f"[Audio Error] Could not open audio stream: {audio_error}")
                time.sleep(2)
                continue  # Retry the loop

        except Exception as e:
            if "Exceeded maximum allowed stream duration" in str(e):
                continue
            else:
                raise e


def choose_color(subtitle_color_var, button):
    color = colorchooser.askcolor(title="Choose Subtitle Color")[1]
    if color:
        subtitle_color_var.set(color)
        button.config(bg=color)


def show_settings():
    settings_root = tk.Tk()
    settings_root.title("Settings")

    direction_var = tk.StringVar(value="fr_to_en")
    subtitle_color_var = tk.StringVar(value="white")
    # Display the default keybind that stops transcription; user can change it.
    stop_key_var = tk.StringVar(value="Alt-F11")

    # List available input devices using PyAudio.
    pya_instance = pyaudio.PyAudio()
    devices = {}
    default_device_name = None
    try:
        default_info = pya_instance.get_default_input_device_info()
        default_device_name = default_info["name"]
    except Exception:
        pass

    for i in range(pya_instance.get_device_count()):
        info = pya_instance.get_device_info_by_index(i)
        if info.get("maxInputChannels", 0) > 0:
            devices[info["name"]] = info["index"]
    pya_instance.terminate()

    device_names = list(devices.keys())
    if default_device_name is None and device_names:
        default_device_name = device_names[0]

    input_device_var = tk.StringVar(value=default_device_name)

    tk.Label(settings_root, text="Select Translation Direction:").pack(pady=5)
    tk.Radiobutton(settings_root, text="French to English", variable=direction_var, value="fr_to_en").pack()
    tk.Radiobutton(settings_root, text="English to French", variable=direction_var, value="en_to_fr").pack()

    tk.Label(settings_root, text="Subtitle Color:").pack(pady=5)
    color_frame = tk.Frame(settings_root)
    color_frame.pack()
    color_button = tk.Button(color_frame, text="Choose Color",
                             command=lambda: choose_color(subtitle_color_var, color_button))
    color_button.pack()

    tk.Label(settings_root, text="Select Input Device:").pack(pady=5)
    device_menu = tk.OptionMenu(settings_root, input_device_var, *device_names)
    device_menu.pack(pady=5)

    tk.Label(settings_root, text="Stop Transcription Key (e.g., Alt-F11):").pack(pady=5)
    tk.Entry(settings_root, textvariable=stop_key_var, state="disabled").pack(pady=5)

    tk.Button(settings_root, text="OK", command=settings_root.destroy).pack(pady=10)

    def on_close():
        stop_transcription_thread()
        settings_root.destroy()
        sys.exit(0)

    settings_root.protocol("WM_DELETE_WINDOW", on_close)
    settings_root.mainloop()

    direction = direction_var.get()
    subtitle_color = subtitle_color_var.get()
    selected_device_name = input_device_var.get()
    input_device_index = devices.get(selected_device_name, None)
    chosen_stop_key = stop_key_var.get()

    if direction == "fr_to_en":
        source_language_code = "fr-BE"
        target_language_code = "en"
    else:
        source_language_code = "en"
        target_language_code = "fr-BE"

    return source_language_code, target_language_code, subtitle_color, input_device_index, chosen_stop_key


def start_transcription_thread(source_language_code, target_language_code, input_device_index):
    global transcription_thread, transcript_queue
    transcript_queue = queue.Queue()
    stop_event.clear()
    transcription_thread = threading.Thread(
        target=run_transcription,
        args=(transcript_queue, source_language_code, target_language_code, input_device_index)
    )
    transcription_thread.daemon = True
    transcription_thread.start()


def stop_transcription_thread():
    global transcription_thread
    stop_event.set()
    if transcription_thread is not None:
        transcription_thread.join(timeout=2)
        if transcription_thread.is_alive():
            print("Warning: Transcription thread did not terminate in time.")
        transcription_thread = None


def create_overlay(subtitle_color, chosen_stop_key):
    # Create a borderless, transparent overlay window for subtitles.
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.config(bg="black")
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

    # Force focus.
    root.after(100, lambda: root.focus_force())

    # No local key binding is required since the global hotkey will work even if the overlay isn't focused.

    def poll_queue():
        try:
            while True:
                message = transcript_queue.get_nowait()
                if message is None:
                    root.quit()
                    return
                subtitle_label.config(text=message)
        except queue.Empty:
            pass
        root.after(25, poll_queue)

    poll_queue()
    return root


def global_stop_handler():
    print("Global hotkey triggered: Alt-F11. Exiting...")
    stop_transcription_thread()
    try:
        global overlay
        if overlay is not None:
            overlay.destroy()
    except Exception as e:
        print("Error closing overlay:", e)
    sys.exit(0)


def main():
    global overlay
    overlay = None
    # Register the global hotkey for Alt-F11.
    keyboard.add_hotkey('alt+f11', global_stop_handler)

    # Main loop that cycles between settings and overlay.
    while True:
        config = show_settings()
        if not config:
            break
        source_language_code, target_language_code, subtitle_color, input_device_index, chosen_stop_key = config

        # Start transcription.
        start_transcription_thread(source_language_code, target_language_code, input_device_index)

        # Create the overlay window.
        overlay = create_overlay(subtitle_color, chosen_stop_key)
        overlay.mainloop()  # Runs until the overlay is destroyed.

        stop_transcription_thread()
        # Loop back to show settings again.


if __name__ == "__main__":
    main()
