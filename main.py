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

# Global settings for language & color
selected_translation = "fr-en"  # Default: French to English
subtitle_color = "white"  # Default subtitle color
exit_flag = False  # Global flag to signal exit


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
            # Check if transcript is empty; if so, skip translation
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

    # Only signal termination if we're actually exiting
    if exit_flag:
        transcript_queue.put(None)


def run_transcription(transcript_queue):
    """Runs the speech recognition and passes results to the queue.
       This version restarts the microphone stream for each utterance (final result)
       by setting single_utterance=True.
    """
    global exit_flag
    client = speech.SpeechClient()

    while not exit_flag:
        source_language, _ = selected_translation.split("-")
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=RATE,
            language_code=source_language,  # Adjust dynamically
            enable_automatic_punctuation=True,
        )
        # Force end-of-utterance detection by enabling single_utterance
        streaming_config = speech.StreamingRecognitionConfig(
            config=config, interim_results=True, single_utterance=True
        )

        with MicrophoneStream(RATE, CHUNK) as stream:
            audio_generator = stream.generator()
            requests = (
                speech.StreamingRecognizeRequest(audio_content=content)
                for content in audio_generator
            )
            responses = client.streaming_recognize(streaming_config, requests)
            listen_print_loop(responses, transcript_queue)


def settings_window(root):
    """Pop-up window for selecting language direction and subtitle color."""
    global selected_translation, subtitle_color

    def apply_settings():
        global selected_translation, subtitle_color
        selected_translation = lang_var.get()
        subtitle_color = color_var.get()
        settings.destroy()  # Close settings before starting subtitles

    def choose_color():
        color_code = colorchooser.askcolor(title="Choose Text Color")[1]
        if color_code:
            color_var.set(color_code)

    settings = tk.Toplevel(root)
    settings.title("Settings")
    settings.geometry("300x200")

    tk.Label(settings, text="Select Translation Mode:").pack(pady=5)

    lang_var = tk.StringVar(value=selected_translation)
    tk.Radiobutton(settings, text="French ➝ English", variable=lang_var, value="fr-en").pack()
    tk.Radiobutton(settings, text="English ➝ French", variable=lang_var, value="en-fr").pack()

    tk.Label(settings, text="Choose Subtitle Color:").pack(pady=5)

    color_var = tk.StringVar(value=subtitle_color)
    tk.Button(settings, text="Pick a Color", command=choose_color).pack(pady=5)

    tk.Button(settings, text="Apply", command=apply_settings).pack(pady=10)

    settings.grab_set()  # Prevents interaction with the main window


def main():
    """Main application logic."""
    root = tk.Tk()
    root.withdraw()  # Hide main window until settings are selected

    settings_window(root)  # Open settings first

    root.deiconify()  # Show the main window after settings are closed

    transcript_queue = queue.Queue()
    transcription_thread = threading.Thread(target=run_transcription, args=(transcript_queue,))
    transcription_thread.start()

    # Create a borderless, transparent overlay window for subtitles.
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
        fg=subtitle_color,  # Apply user-selected color
        bg="black",
        wraplength=screen_width - 100,
        justify="center"
    )
    subtitle_label.pack(expand=True, fill="both")

    def poll_queue():
        """Checks the transcript queue and updates subtitles in real-time."""
        try:
            while True:
                message = transcript_queue.get_nowait()
                if message is None:
                    root.quit()
                    return
                subtitle_label.config(text=message, fg=subtitle_color)  # Update text color
        except queue.Empty:
            pass
        root.after(25, poll_queue)  # Faster polling for better UI updates

    poll_queue()
    root.mainloop()
    transcription_thread.join()


if __name__ == "__main__":
    main()
