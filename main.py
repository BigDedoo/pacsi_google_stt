import sys
import os
import time  # Added to allow a delay before restarting the audio stream
import keyboard  # For global hotkeys
import html
import queue
import re
import threading
from PyQt5 import QtWidgets, QtGui, QtCore  # Using PyQt for settings dialog
import pyaudio
from google.cloud import speech, translate


# If running as a bundled executable, set the credentials path to the extracted file.
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(".")

credentials_path = os.path.join(base_path, "stttesting-445210-aa5e435ad2b1.json")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path


# Audio recording parameters
RATE = 16000
CHUNK = RATE // 20  # 50ms updates

# Global control event and transcription thread holder.
stop_event = threading.Event()
transcription_thread = None


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

    # Drop any empty or whitespace-only entries before calling the API
    nonempty = [t for t in texts if t.strip()]
    if not nonempty:
        # Nothing to translate; return originals
        return texts[:]

    response = client.translate_text(
        request={
            "contents": nonempty,
            "target_language_code": target_language,
            "parent": parent,
        }
    )
    translated = [html.unescape(tr.translated_text) for tr in response.translations]

    # Rebuild the output list, replacing only the non-empty slots
    out = []
    ti = 0
    for t in texts:
        if t.strip():
            out.append(translated[ti])
            ti += 1
        else:
            out.append(t)
    return out


def log_translation(source: str, translation: str, filename: str = "translation_log.txt") -> None:
    """Append the original text and its translation to a log file."""
    with open(filename, "a", encoding="utf-8") as f:
        f.write(f"Source: {source}\nTranslation: {translation}\n\n")


def listen_print_loop(responses, transcript_queue, target_language_code):
    # Throttle interval for translating interim results (in seconds)
    TRANSLATION_INTERVAL = 1.0
    last_translate_time = time.time() - TRANSLATION_INTERVAL
    last_translated_text = ""
    num_chars_printed = 0

    def sentence_finished(text):
        # Check if the text, once trimmed, ends with punctuation indicating sentence completion.
        text = text.strip()
        return text.endswith('.') or text.endswith('!') or text.endswith('?')

    for response in responses:
        if stop_event.is_set():
            break
        if not response.results:
            continue
        result = response.results[0]
        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript
        # Skip completely empty transcripts
        if not transcript.strip():
            continue

        # Trigger immediate translation if the result is final or the sentence appears finished.
        if result.is_final or sentence_finished(transcript):
            translations = translate_text([transcript], target_language=target_language_code)
            translated_text = translations[0] if translations else transcript

            # Log the original transcript and its translation.
            log_translation(transcript, translated_text)

            transcript_queue.put(translated_text)

            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                transcript_queue.put("Exiting..")
                return True  # signal to exit

            num_chars_printed = 0
            last_translated_text = translated_text
            last_translate_time = time.time()
        else:
            current_time = time.time()
            # Use throttled translation for interim updates that don't indicate sentence completion.
            if current_time - last_translate_time >= TRANSLATION_INTERVAL:
                translations = translate_text([transcript], target_language=target_language_code)
                translated_text = translations[0] if translations else transcript
                last_translate_time = current_time
                last_translated_text = translated_text
                num_chars_printed = len(translated_text)
            else:
                translated_text = last_translated_text if last_translated_text else transcript

            overwrite_chars = (
                " " * (num_chars_printed - len(translated_text))
                if num_chars_printed > len(translated_text)
                else ""
            )
            transcript_queue.put(translated_text + overwrite_chars)

    transcript_queue.put(None)
    return False


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
    import tkinter as tk  # Still using Tkinter for the overlay
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
    window_height = 200
    x_pos = 0
    y_pos = screen_height - window_height - 50
    root.geometry(f"{screen_width}x{window_height}+{x_pos}+{y_pos}")

    # Left-aligned label
    subtitle_label = tk.Label(
        root,
        text="",
        font=("Helvetica", 28),
        fg=subtitle_color,
        bg="black",
        wraplength=screen_width - 100,
        justify="left",    # now left-aligned
        anchor="w"         # text anchored to the west
    )
    subtitle_label.place(
        relx=0.0,           # start at the left edge
        rely=0.5,
        anchor="w",
        width=screen_width - 100,
        height=window_height
    )

    def poll_queue():
        try:
            while True:
                message = transcript_queue.get_nowait()
                if message is None:
                    if root.winfo_exists():
                        root.quit()
                    return
                # Split into sentences and take only one segment
                segments = re.split(r'(?<=[\.!?])\s+', message.strip())
                to_display = segments[-1] if segments else message  # one sentence max
                subtitle_label.config(text=to_display)
        except queue.Empty:
            pass
        try:
            if root.winfo_exists():
                root.after(25, poll_queue)
        except tk.TclError:
            pass

    poll_queue()
    return root


def global_stop_handler():
    print("Global hotkey triggered: Alt-F11. Exiting application.")
    os._exit(0)


def show_settings():
    """
    Displays a PyQt-based settings dialog.
    Returns:
      (source_language_code, target_language_code, subtitle_color, input_device_index, chosen_stop_key)
      or None if the dialog was cancelled.
    """
    # Create a QApplication if one does not exist.
    app = QtWidgets.QApplication.instance()
    if app is None:
        app = QtWidgets.QApplication(sys.argv)

    class SettingsDialog(QtWidgets.QDialog):
        def __init__(self, parent=None):
            super(SettingsDialog, self).__init__(parent)
            self.setWindowTitle("Settings")
            self.setModal(True)
            self.resize(400, 300)
            layout = QtWidgets.QVBoxLayout()

            # Translation Direction
            layout.addWidget(QtWidgets.QLabel("Select Translation Direction:"))
            self.radio_fr_to_en = QtWidgets.QRadioButton("French to English")
            self.radio_en_to_fr = QtWidgets.QRadioButton("English to French")
            self.radio_fr_to_en.setChecked(True)
            layout.addWidget(self.radio_fr_to_en)
            layout.addWidget(self.radio_en_to_fr)

            # Subtitle Color with preview
            self.subtitle_color = "white"
            color_layout = QtWidgets.QHBoxLayout()
            color_layout.addWidget(QtWidgets.QLabel("Subtitle Color:"))

            # Add a color preview label
            self.color_preview = QtWidgets.QLabel()
            self.color_preview.setFixedSize(40, 20)
            self.color_preview.setStyleSheet("background-color: white; border: 1px solid black;")
            color_layout.addWidget(self.color_preview)

            self.color_button = QtWidgets.QPushButton("Choose Color")
            self.color_button.clicked.connect(self.choose_color)
            color_layout.addWidget(self.color_button)
            layout.addLayout(color_layout)

            # Input Device Selection
            layout.addWidget(QtWidgets.QLabel("Select Input Device:"))
            self.input_device_combo = QtWidgets.QComboBox()
            self.devices = {}
            pya_instance = pyaudio.PyAudio()
            default_device_name = None
            try:
                default_info = pya_instance.get_default_input_device_info()
                default_device_name = default_info["name"]
            except Exception:
                pass

            for i in range(pya_instance.get_device_count()):
                info = pya_instance.get_device_info_by_index(i)
                if info.get("maxInputChannels", 0) > 0:
                    device_name = info["name"]
                    self.devices[device_name] = info["index"]
                    self.input_device_combo.addItem(device_name)
            pya_instance.terminate()
            layout.addWidget(self.input_device_combo)

            # Stop Transcription Key (read-only)
            layout.addWidget(QtWidgets.QLabel("Stop Transcription Key (e.g., Alt-F11):"))
            self.stop_key = "Alt-F11"
            self.stop_key_edit = QtWidgets.QLineEdit(self.stop_key)
            self.stop_key_edit.setReadOnly(True)
            layout.addWidget(self.stop_key_edit)

            # OK Button
            self.ok_button = QtWidgets.QPushButton("OK")
            self.ok_button.clicked.connect(self.accept)
            layout.addWidget(self.ok_button)

            self.setLayout(layout)

        def choose_color(self):
            # Pass self as the parent to ensure the color dialog stays on top of the settings window.
            color = QtWidgets.QColorDialog.getColor(parent=self)
            if color.isValid():
                self.subtitle_color = color.name()
                self.color_preview.setStyleSheet(
                    f"background-color: {self.subtitle_color}; border: 1px solid black;"
                )

    dialog = SettingsDialog()
    dialog.setWindowFlags(dialog.windowFlags() | QtCore.Qt.WindowStaysOnTopHint)
    dialog.show()
    dialog.raise_()
    result = dialog.exec_()

    if result == QtWidgets.QDialog.Accepted:
        direction = "fr_to_en" if dialog.radio_fr_to_en.isChecked() else "en_to_fr"
        subtitle_color = dialog.subtitle_color
        selected_device_name = dialog.input_device_combo.currentText()
        input_device_index = dialog.devices.get(selected_device_name, None)
        chosen_stop_key = dialog.stop_key
        if direction == "fr_to_en":
            source_language_code = "fr-BE"
            target_language_code = "en"
        else:
            source_language_code = "en-US"
            target_language_code = "fr-BE"
        return source_language_code, target_language_code, subtitle_color, input_device_index, chosen_stop_key
    else:
        return None


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
