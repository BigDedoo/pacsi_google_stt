#!/usr/bin/env python3
import sys
import os
import re
import queue
import threading
import logging
import html

import pyaudio
import tkinter as tk
import keyboard
from PyQt5 import QtWidgets
from google.cloud import speech, translate_v2 as translate
from google.api_core.exceptions import GoogleAPIError

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
# read from mic every 100 ms (instead of 2 s)
CHUNK = RATE // 10  # 100 ms

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

        # Translation direction
        layout.addWidget(QtWidgets.QLabel("Select Translation Direction:"))
        self.radio_fr_to_en = QtWidgets.QRadioButton("French to English")
        self.radio_en_to_fr = QtWidgets.QRadioButton("English to French")
        self.radio_fr_to_en.setChecked(True)
        layout.addWidget(self.radio_fr_to_en)
        layout.addWidget(self.radio_en_to_fr)

        # Subtitle color
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

        # Input device selection
        layout.addWidget(QtWidgets.QLabel("Select Input Device:"))
        self.input_device_combo = QtWidgets.QComboBox()
        self.devices = {}
        p = pyaudio.PyAudio()
        try:
            default_info = p.get_default_input_device_info()
            default_name = default_info.get("name")
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

        # Global stop key display
        layout.addWidget(QtWidgets.QLabel("Global Stop Key:"))
        self.stop_key = "alt+f11"
        self.stop_key_edit = QtWidgets.QLineEdit(self.stop_key)
        self.stop_key_edit.setReadOnly(True)
        layout.addWidget(self.stop_key_edit)

        # OK/Cancel buttons
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
        direction = ("fr-FR", "en") if self.radio_fr_to_en.isChecked() else ("en-US", "fr-FR")
        device = self.devices.get(self.input_device_combo.currentText())
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
    def __init__(self, subtitle_color, poll_interval=50):
        super().__init__()
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.config(bg="black")
        try:
            self.wm_attributes("-transparentcolor", "black")
        except tk.TclError:
            pass
        w, h = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x200+0+{h-250}")
        self.label = tk.Label(
            self, text="", font=("Helvetica", 28),
            fg=subtitle_color, bg="black",
            wraplength=w-100, justify="left", anchor="w"
        )
        self.label.place(relx=0, rely=0.5, anchor="w", width=w-100, height=200)
        self.last_displayed = None

        # store and use a tighter poll interval
        self.poll_interval = poll_interval
        self.after(self.poll_interval, self._poll_queue)

    def _poll_queue(self):
        latest = None
        while True:
            try:
                txt = result_queue.get_nowait()
            except queue.Empty:
                break

            if txt is None:
                self.destroy()
                return
            latest = txt

        if latest is not None and latest != self.last_displayed:
            self.label.config(text=latest)
            self.last_displayed = latest

        self.after(self.poll_interval, self._poll_queue)

# ----------------------------------------
# MICROPHONE STREAM
# ----------------------------------------
class MicrophoneStream:
    def __init__(self, rate, chunk, device_index=None):
        self.rate, self.chunk, self.device = rate, chunk, device_index
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        self.audio_interface = pyaudio.PyAudio()
        self.audio_stream = self.audio_interface.open(
            format=pyaudio.paInt16, channels=1, rate=self.rate,
            input=True, input_device_index=self.device,
            frames_per_buffer=self.chunk, stream_callback=self._fill_buffer)
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
                    c = self._buff.get(block=False)
                except queue.Empty:
                    break
                if c is None:
                    return
                data.append(c)
            yield b"".join(data)

# ----------------------------------------
# TRANSCRIBER THREAD WITH CONFIDENCE LOGGING
# ----------------------------------------
class Transcriber(threading.Thread):
    def __init__(self, src, tgt, device):
        super().__init__(daemon=True)
        self.src = src
        self.tgt = tgt
        self.device = device
        self.stop_event = threading.Event()
        self.speech = speech.SpeechClient()
        self.translate = translate.Client()

    def _translate(self, txt):
        try:
            res = self.translate.translate(txt, target_language=self.tgt)
            return html.unescape(res.get("translatedText", txt))
        except Exception as e:
            logging.error("Translation error: %s", e)
            return txt

    def run(self):
        cfg = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=RATE,
            language_code=self.src,
            enable_automatic_punctuation=True,
            enable_word_confidence=True,
            model="phone_call",
            use_enhanced=True
        )
        stream_cfg = speech.StreamingRecognitionConfig(
            config=cfg,
            interim_results=True
        )
        with MicrophoneStream(RATE, CHUNK, self.device) as mic:
            requests = (speech.StreamingRecognizeRequest(audio_content=chunk)
                        for chunk in mic.generator())
            for resp in self.speech.streaming_recognize(stream_cfg, requests):
                if self.stop_event.is_set():
                    break
                if not resp.results or not resp.results[0].alternatives:
                    continue
                result = resp.results[0]
                alt = result.alternatives[0]
                text = alt.transcript.strip()
                confidence = getattr(alt, 'confidence', None)
                if not text:
                    continue

                # display interim & final translations immediately
                translated = self._translate(text)
                if result.is_final:
                    logging.info("Final sentence: %s", translated)
                else:
                    logging.info("Interim result: %s", translated)
                result_queue.put(translated)

        result_queue.put(None)

    def stop(self):
        self.stop_event.set()

# ----------------------------------------
# GLOBAL STOP
# ----------------------------------------
def global_stop():
    os._exit(0)

# ----------------------------------------
# MAIN
# ----------------------------------------
def main():
    app = QtWidgets.QApplication(sys.argv)
    while True:
        dlg = SettingsDialog()
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            break
        cfg = dlg.get_settings()
        keyboard.unhook_all()
        keyboard.add_hotkey(cfg["stop_key"], global_stop)
        trans = Transcriber(cfg["source_lang"], cfg["target_lang"], cfg["input_device_index"])
        trans.start()
        # poll every 50 ms for maximum responsiveness
        SubtitleOverlay(cfg["subtitle_color"], poll_interval=50).mainloop()
        trans.stop()
        trans.join()

if __name__ == "__main__":
    main()
