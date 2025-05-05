#!/usr/bin/env python3
import sys
import os
import re
import queue
import threading
import logging
import time
import html

import pyaudio
import tkinter as tk
import keyboard
from PyQt5 import QtWidgets
from google.cloud import speech, translate_v2 as translate
from google.api_core.exceptions import GoogleAPIError

# ----------------------------------------
# HELPER: Extract only the last sentence
# ----------------------------------------
def extract_last_sentence(text: str) -> str:
    """
    Split on end-of-sentence punctuation (., !, ?) plus whitespace,
    and return only the final segment. Falls back to the whole text.
    """
    segments = re.split(r'(?<=[.!?])\s+', text.strip())
    return segments[-1] if segments else text

# ----------------------------------------
# CONFIGURATION
# ----------------------------------------
if getattr(sys, 'frozen', False):
    base_path = sys._MEIPASS
else:
    base_path = os.path.abspath(".")
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.path.join(
    base_path, "stttesting-445210-aa5e435ad2b1.json"
)

RATE = 48000
CHUNK = RATE // 5  # 200 ms

# ----------------------------------------
# LOGGING SETUP
# ----------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ----------------------------------------
# THREAD-SAFE QUEUE FOR SUBTITLES
# ----------------------------------------
result_queue = queue.Queue()

# ----------------------------------------
# SETTINGS DIALOG (PyQt5)
# ----------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.resize(400, 300)
        layout = QtWidgets.QVBoxLayout(self)

        # Translation Direction
        layout.addWidget(QtWidgets.QLabel("Select Translation Direction:"))
        self.radio_fr_to_en = QtWidgets.QRadioButton("French to English")
        self.radio_en_to_fr = QtWidgets.QRadioButton("English to French")
        self.radio_fr_to_en.setChecked(True)
        layout.addWidget(self.radio_fr_to_en)
        layout.addWidget(self.radio_en_to_fr)

        # Subtitle Color
        self.subtitle_color = "#FFFFFF"
        color_layout = QtWidgets.QHBoxLayout()
        color_layout.addWidget(QtWidgets.QLabel("Subtitle Color:"))
        self.color_preview = QtWidgets.QLabel()
        self.color_preview.setFixedSize(40, 20)
        self.color_preview.setStyleSheet(f"background-color: {self.subtitle_color}; border: 1px solid black;")
        color_layout.addWidget(self.color_preview)
        self.color_button = QtWidgets.QPushButton("Choose Color")
        self.color_button.clicked.connect(self.choose_color)
        color_layout.addWidget(self.color_button)
        layout.addLayout(color_layout)

        # Input Device Selection
        layout.addWidget(QtWidgets.QLabel("Select Input Device:"))
        self.input_device_combo = QtWidgets.QComboBox()
        self.devices = {}
        p = pyaudio.PyAudio()
        try:
            default_info = p.get_default_input_device_info()
            default_name = default_info["name"]
        except Exception:
            default_name = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0:
                name = info["name"]
                self.devices[name] = i
                self.input_device_combo.addItem(name)
                if name == default_name:
                    self.input_device_combo.setCurrentText(name)
        p.terminate()
        layout.addWidget(self.input_device_combo)

        # Stop Key (read-only)
        layout.addWidget(QtWidgets.QLabel("Global Stop Key:"))
        self.stop_key = "alt+f11"
        self.stop_key_edit = QtWidgets.QLineEdit(self.stop_key)
        self.stop_key_edit.setReadOnly(True)
        layout.addWidget(self.stop_key_edit)

        # OK / Cancel buttons
        btn_layout = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton("OK")
        ok.clicked.connect(self.accept)
        btn_layout.addWidget(ok)
        cancel = QtWidgets.QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        btn_layout.addWidget(cancel)
        layout.addLayout(btn_layout)

    def choose_color(self):
        color = QtWidgets.QColorDialog.getColor(parent=self)
        if color.isValid():
            self.subtitle_color = color.name()
            self.color_preview.setStyleSheet(f"background-color: {self.subtitle_color}; border: 1px solid black;")

    def get_settings(self):
        direction = ("fr-BE", "en") if self.radio_fr_to_en.isChecked() else ("en-US", "fr-BE")
        device = self.devices.get(self.input_device_combo.currentText(), None)
        return {
            "source_lang": direction[0],
            "target_lang": direction[1],
            "subtitle_color": self.subtitle_color,
            "input_device_index": device,
            "stop_key": self.stop_key,
        }

# ----------------------------------------
# OVERLAY WINDOW (Tkinter)
# ----------------------------------------
class SubtitleOverlay(tk.Tk):
    def __init__(self, subtitle_color, poll_interval=100):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.config(bg="black")
        try:
            self.wm_attributes("-transparentcolor", "black")
        except tk.TclError:
            pass

        w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        win_h = 200
        self.geometry(f"{w}x{win_h}+0+{h - win_h - 50}")

        self.label = tk.Label(self, text="", font=("Helvetica", 28),
                              fg=subtitle_color, bg="black",
                              wraplength=w - 100, justify="left", anchor="w")
        self.label.place(relx=0, rely=0.5, anchor="w", width=w - 100, height=win_h)
        self.after(poll_interval, self._poll_queue)

    def _poll_queue(self):
        try:
            while True:
                text = result_queue.get_nowait()
                if text is None:
                    self.destroy()
                    return
                self.label.config(text=text)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

# ----------------------------------------
# MICROPHONE STREAM (PyAudio)
# ----------------------------------------
class MicrophoneStream:
    def __init__(self, rate, chunk, device_index=None):
        self.rate = rate
        self.chunk = chunk
        self.device_index = device_index
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self.audio_interface = pyaudio.PyAudio()
        try:
            self.audio_stream = self.audio_interface.open(
                format=pyaudio.paInt16, channels=1, rate=self.rate,
                input=True, input_device_index=self.device_index,
                frames_per_buffer=self.chunk, stream_callback=self._fill_buffer
            )
        except Exception as e:
            logging.error("Failed to open audio stream: %s", e)
            raise
        self.closed = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.closed = True
        self.audio_stream.stop_stream()
        self.audio_stream.close()
        self.audio_interface.terminate()
        self._buff.put(None)

    def _fill_buffer(self, in_data, frame_count, time_info, status):
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

# ----------------------------------------
# TRANSCRIBER THREAD (with interim + last-sentence)
# ----------------------------------------
class Transcriber(threading.Thread):
    def __init__(self, src_lang, tgt_lang, device_index):
        super().__init__(daemon=True)
        self.src, self.tgt, self.device = src_lang, tgt_lang, device_index
        self.stop_event = threading.Event()
        self.speech_client = speech.SpeechClient()
        self.translate_client = translate.Client()

        self.translation_interval = 0.8
        self.last_interim_time = time.time() - self.translation_interval
        self.last_interim_text = ""

    def run(self):
        config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=RATE,
            language_code=self.src,
            enable_automatic_punctuation=True,
        )
        streaming_cfg = speech.StreamingRecognitionConfig(config=config, interim_results=True)
        logging.info("Transcriber started.")
        try:
            with MicrophoneStream(RATE, CHUNK, self.device) as mic:
                audio_gen = mic.generator()
                requests = (speech.StreamingRecognizeRequest(audio_content=chunk)
                            for chunk in audio_gen)
                responses = self.speech_client.streaming_recognize(streaming_cfg, requests)

                for resp in responses:
                    if self.stop_event.is_set():
                        logging.info("Stop event set; breaking.")
                        break
                    self._handle_response(resp)

        except Exception:
            logging.exception("Fatal error in Transcriber:")
        finally:
            result_queue.put(None)
            logging.info("Transcriber exiting.")

    def stop(self):
        logging.info("Transcriber.stop() called.")
        self.stop_event.set()

    def _handle_response(self, resp):
        if not resp.results or not resp.results[0].alternatives:
            return
        alt = resp.results[0].alternatives[0]
        transcript = alt.transcript.strip()
        if not transcript:
            return

        now = time.time()
        is_final = resp.results[0].is_final

        if is_final:
            full = self._translate_text(transcript)
            last = extract_last_sentence(full)
            logging.info("Final: %r → %r", transcript, last)
            result_queue.put(last)
            self.last_interim_time = now
            self.last_interim_text = ""
        else:
            if now - self.last_interim_time >= self.translation_interval:
                full = self._translate_text(transcript)
                last = extract_last_sentence(full)
                self.last_interim_text = last
                self.last_interim_time = now
            else:
                last = self.last_interim_text or extract_last_sentence(transcript)
            result_queue.put(last)

    def _translate_text(self, text):
        try:
            res = self.translate_client.translate(text, target_language=self.tgt)
            raw = res.get("translatedText", text)
            return html.unescape(raw)
        except GoogleAPIError as e:
            logging.error("Translation API error: %s", e)
            return text
        except Exception:
            logging.exception("Unexpected translation error:")
            return text

# ----------------------------------------
# GLOBAL STOP HANDLER
# ----------------------------------------
def global_stop():
    logging.info("Global stop triggered. Exiting immediately.")
    os._exit(0)

# ----------------------------------------
# MAIN LOOP
# ----------------------------------------
def main():
    app = QtWidgets.QApplication(sys.argv)

    while True:
        dlg = SettingsDialog()
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            break
        cfg = dlg.get_settings()

        try:
            keyboard.unhook_all()
        except Exception:
            logging.warning("Could not unhook previous hotkeys.")
        keyboard.add_hotkey(cfg["stop_key"], global_stop)
        logging.info("Registered global stop hotkey: %s", cfg["stop_key"])

        transcriber = Transcriber(cfg["source_lang"], cfg["target_lang"], cfg["input_device_index"])
        transcriber.start()

        overlay = SubtitleOverlay(cfg["subtitle_color"])
        overlay.mainloop()

        transcriber.stop()
        transcriber.join(timeout=2)

    logging.info("Settings cancelled. Application exiting.")

if __name__ == "__main__":
    main()
