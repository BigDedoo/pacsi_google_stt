import html
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import colorchooser

from google.cloud import speech, translate
import pyaudio

# Audio recording parameters
RATE = 16000
CHUNK = RATE // 20  # Reduced chunk size for lower latency (50ms updates)


class MicrophoneStream:
    """Opens a recording stream as a generator yielding audio chunks."""

    def __init__(self, rate: int = RATE, chunk: int = CHUNK) -> None:
        self._rate = rate
        self._chunk = chunk
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self) -> "MicrophoneStream":
        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self._rate,
            input=True,
            frames_per_buffer=self._chunk,
            stream_callback=self._fill_buffer,
        )
        self.closed = False
        return self

    def __exit__(self, type, value, traceback) -> None:
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, frame_count, time_info, status_flags):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            chunk = self._buff.get()
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

    for response in responses:
        if not response.results:
            continue

        result = response.results[0]
        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript
        overwrite_chars = " " * (num_chars_printed - len(transcript))

        if not result.is_final:
            # For interim results, send the transcript as is.
            transcript_queue.put(transcript + overwrite_chars)
            num_chars_printed = len(transcript)
        else:
            # Translate the final transcript.
            translations = translate_text([transcript], target_language=target_language_code)
            translated_text = translations[0] if translations else transcript
            transcript_queue.put(translated_text)

            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                transcript_queue.put("Exiting..")
                break

            num_chars_printed = 0

    transcript_queue.put(None)


def run_transcription(transcript_queue, source_language_code, target_language_code):
    """Runs the speech recognition and passes results to the queue."""
    client = speech.SpeechClient()
    config = speech.RecognitionConfig(
        encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
        sample_rate_hertz=RATE,
        language_code=source_language_code,
        enable_automatic_punctuation=True,
    )
    streaming_config = speech.StreamingRecognitionConfig(
        config=config, interim_results=True, single_utterance=False
    )

    with MicrophoneStream(RATE, CHUNK) as stream:
        audio_generator = stream.generator()
        requests = (
            speech.StreamingRecognizeRequest(audio_content=content)
            for content in audio_generator
        )
        responses = client.streaming_recognize(streaming_config, requests)
        listen_print_loop(responses, transcript_queue, target_language_code)


def choose_color(subtitle_color_var, button):
    color = colorchooser.askcolor(title="Choose subtitle color")[1]
    if color:
        subtitle_color_var.set(color)
        button.config(bg=color)


def show_settings():
    settings_root = tk.Tk()
    settings_root.title("Settings")

    direction_var = tk.StringVar(value="fr_to_en")
    subtitle_color_var = tk.StringVar(value="white")

    tk.Label(settings_root, text="Select Translation Direction:").pack(pady=5)
    tk.Radiobutton(settings_root, text="French to English", variable=direction_var, value="fr_to_en").pack()
    tk.Radiobutton(settings_root, text="English to French", variable=direction_var, value="en_to_fr").pack()

    tk.Label(settings_root, text="Subtitle Color:").pack(pady=5)
    color_frame = tk.Frame(settings_root)
    color_frame.pack()
    color_button = tk.Button(color_frame, text="Choose Color",
                             command=lambda: choose_color(subtitle_color_var, color_button))
    color_button.pack()

    def on_ok():
        settings_root.quit()

    tk.Button(settings_root, text="OK", command=on_ok).pack(pady=10)

    settings_root.mainloop()

    direction = direction_var.get()
    subtitle_color = subtitle_color_var.get()

    if direction == "fr_to_en":
        source_language_code = "fr-BE"
        target_language_code = "en"
    else:
        source_language_code = "en"
        target_language_code = "fr-BE"

    settings_root.destroy()
    return source_language_code, target_language_code, subtitle_color


def main():
    # Show the settings window and get user preferences.
    source_language_code, target_language_code, subtitle_color = show_settings()

    transcript_queue = queue.Queue()
    transcription_thread = threading.Thread(
        target=run_transcription,
        args=(transcript_queue, source_language_code, target_language_code)
    )
    transcription_thread.start()

    # Create a borderless, transparent overlay window for subtitles.
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.config(bg="black")

    try:
        root.wm_attributes("-transparentcolor", "black")
    except tk.TclError:
        pass  # Not all systems support transparency.

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
    root.mainloop()
    transcription_thread.join()


if __name__ == "__main__":
    main()
