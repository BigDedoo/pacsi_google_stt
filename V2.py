#!/usr/bin/env python3
import sys
import os
import queue
import threading
import logging
import html
import wave
import argparse
import time
import textwrap
import ctypes
from ctypes import wintypes

import pyaudio
import tkinter as tk
import keyboard
from PyQt5 import QtWidgets, QtGui

# V2 imports
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech as cloud_speech_types

from google.cloud import translate_v2 as translate
from google.api_core import exceptions

import win32gui
import win32api
import win32con

# ----------------------------------------
# CONFIGURATION
# ----------------------------------------
if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.abspath('.')
log_file = os.path.join(base_path, 'app.log')

# ----------------------------------------
# LOGGING SETUP
# ----------------------------------------
log_formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')
file_handler = logging.FileHandler(log_file, mode='a', encoding='utf-8')
file_handler.setFormatter(log_formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(log_formatter)
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logging.error('Uncaught exception', exc_info=(exc_type, exc_value, exc_traceback))
sys.excepthook = handle_exception

os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
    base_path, 'stttesting-445210-aa5e435ad2b1.json'
)

RATE = 48000
DISPLAY_INTERVAL = 2500
CHUNK = int(RATE * (DISPLAY_INTERVAL / 1000.0))
MAX_CHUNK_BYTES = 25600
SLIDE_HEIGHT = 140
STABILITY_THRESHOLD = 0.80  # only take interim results >= this

# queue of (transcript, is_final)
result_queue = queue.Queue()

# supported languages and their display names
LANG_OPTIONS = [
    ('French (France)', 'fr-FR'),
    ('English (US)', 'en-US'),
    ('English (Hong Kong)', 'en-HK'),
    ('English (India)', 'en-IN'),
    ('Russian (Russia)', 'ru-RU'),
    ('Japanese (Japan)', 'ja-JP'),
    ('Spanish (Spain)', 'es-ES'),
    ('Italian (Italy)', 'it-IT'),
    ('German (Germany)', 'de-DE'),
]

# ----------------------------------------
# FILE-BASED “MIC” FOR DEV (WAV only)
# ----------------------------------------
class FileAudioStream:
    def __init__(self, filename, rate, chunk):
        self.filename = filename
        self.rate = rate
        self.chunk = chunk
        self.wav = None

    def __enter__(self):
        self.wav = wave.open(self.filename, 'rb')
        assert self.wav.getnchannels() == 1, 'WAV must be mono'
        assert self.wav.getsampwidth() == 2, 'WAV must be 16-bit'
        assert self.wav.getframerate() == RATE, f'WAV sample rate must be {RATE}'
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.wav.close()

    def generator(self):
        seconds_per_chunk = float(self.chunk) / self.rate
        while True:
            data = self.wav.readframes(self.chunk)
            if not data:
                return
            yield data
            time.sleep(seconds_per_chunk)

# ----------------------------------------
# SETTINGS DIALOG (PyQt5)
# ----------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setModal(True)
        self.resize(400, 250)
        layout = QtWidgets.QVBoxLayout(self)

        # speaker language picker
        layout.addWidget(QtWidgets.QLabel('Speaker Language:'))
        self.src_combo = QtWidgets.QComboBox()
        for name, code in LANG_OPTIONS:
            self.src_combo.addItem(name, code)
        layout.addWidget(self.src_combo)

        # translation language picker
        layout.addWidget(QtWidgets.QLabel('Translate To:'))
        self.tgt_combo = QtWidgets.QComboBox()
        for name, code in LANG_OPTIONS:
            self.tgt_combo.addItem(name, code)
        self.tgt_combo.setCurrentIndex(1)  # default to English (US)
        layout.addWidget(self.tgt_combo)

        # subtitle color
        layout.addWidget(QtWidgets.QLabel('Subtitle Color:'))
        color_layout = QtWidgets.QHBoxLayout()
        self.color_preview = QtWidgets.QLabel()
        self.color_preview.setFixedSize(40, 20)
        self.subtitle_color = '#FFFFFF'
        self.color_preview.setStyleSheet(
            f'background-color: {self.subtitle_color}; border: 1px solid black;'
        )
        color_layout.addWidget(self.color_preview)
        choose_color = QtWidgets.QPushButton('Choose...')
        choose_color.clicked.connect(self.choose_color)
        color_layout.addWidget(choose_color)
        layout.addLayout(color_layout)

        # input device
        layout.addWidget(QtWidgets.QLabel('Select Input Device:'))
        self.input_combo = QtWidgets.QComboBox()
        p = pyaudio.PyAudio()
        default = None
        try:
            default = p.get_default_input_device_info().get('name')
        except:
            pass
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info['name']
                self.input_combo.addItem(name, i)
                if name == default:
                    self.input_combo.setCurrentIndex(self.input_combo.count() - 1)
        p.terminate()
        layout.addWidget(self.input_combo)

        # stop key
        layout.addWidget(QtWidgets.QLabel('Global Stop Key:'))
        self.stop_key = 'alt+f11'
        stop_edit = QtWidgets.QLineEdit(self.stop_key)
        stop_edit.setReadOnly(True)
        layout.addWidget(stop_edit)

        # OK/Cancel
        btns = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton('OK'); ok.clicked.connect(self.accept)
        cancel = QtWidgets.QPushButton('Cancel'); cancel.clicked.connect(self.reject)
        btns.addWidget(ok); btns.addWidget(cancel)
        layout.addLayout(btns)

    def choose_color(self):
        color = QtWidgets.QColorDialog.getColor(parent=self)
        if color.isValid():
            self.subtitle_color = color.name()
            self.color_preview.setStyleSheet(
                f'background-color: {self.subtitle_color}; border: 1px solid black;'
            )

    def get_settings(self):
        return {
            'src_language': self.src_combo.currentData(),
            'tgt_language': self.tgt_combo.currentData(),
            'subtitle_color': self.subtitle_color,
            'input_device_index': self.input_combo.currentData(),
            'stop_key': self.stop_key,
        }

# ----------------------------------------
# OVERLAY WINDOW
# ----------------------------------------
class SubtitleOverlay(tk.Tk):
    MAX_WORDS = 50

    def __init__(self, subtitle_color, poll_interval, fixed_tgt):
        super().__init__()
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.config(bg='black')
        try:
            self.wm_attributes('black')
        except tk.TclError:
            pass

        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f'{sw}x{SLIDE_HEIGHT}+0+{sh-SLIDE_HEIGHT}')
        self.after(0, lambda: self._register_appbar(self.winfo_id(), SLIDE_HEIGHT))
        self._hook_fullscreen()

        self.label = tk.Label(
            self, text='', font=('Helvetica', 28),
            fg=subtitle_color, bg='black',
            wraplength=sw-100, justify='left', anchor='w'
        )
        self.label.place(relx=0, rely=0.5, anchor='w', width=sw-100, height=SLIDE_HEIGHT)

        self.poll_interval = poll_interval
        self.translate_client = translate.Client()
        self.translate_call_count = 0
        self.translate_char_count = 0
        self.fixed_tgt = fixed_tgt

        self.final_words = []
        self.current_interim = ''
        self.after(self.poll_interval, self._poll_queue)

    def _register_appbar(self, hwnd, height):
        ABM_NEW, ABM_QUERYPOS, ABM_SETPOS = 0, 2, 3
        ABE_BOTTOM = 3
        class APPBARDATA(ctypes.Structure):
            _fields_ = [
                ('cbSize', wintypes.DWORD),
                ('hWnd', wintypes.HWND),
                ('uCallbackMessage', wintypes.UINT),
                ('uEdge', wintypes.UINT),
                ('rc', wintypes.RECT),
                ('lParam', wintypes.LPARAM),
            ]
        abd = APPBARDATA()
        abd.cbSize = ctypes.sizeof(abd)
        abd.hWnd = hwnd
        abd.uEdge = ABE_BOTTOM
        ctypes.windll.shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        abd.rc.left, abd.rc.right = 0, screen_w
        abd.rc.top, abd.rc.bottom = screen_h-height, screen_h
        ctypes.windll.shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))

    def _hook_fullscreen(self):
        def shrink_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            r = win32gui.GetWindowRect(hwnd)
            mon = win32api.GetMonitorInfo(win32api.MonitorFromWindow(hwnd))['Monitor']
            if r == mon:
                l, t, rw, b = mon
                win32gui.SetWindowPos(
                    hwnd, None, l, t, rw-l, (b-t)-SLIDE_HEIGHT, win32con.SWP_NOZORDER
                )
        win32gui.EnumWindows(shrink_cb, None)
        self.after(1000, self._hook_fullscreen)

    def _poll_queue(self):
        item = None
        while True:
            try:
                item = result_queue.get_nowait()
            except queue.Empty:
                break

        if not item:
            self.after(self.poll_interval, self._poll_queue)
            return

        text, is_final = item
        if not text:
            self.after(self.poll_interval, self._poll_queue)
            return

        self.translate_call_count += 1
        self.translate_char_count += len(text)
        logging.info(f'Translate API call #{self.translate_call_count}, chars sent: {len(text)}')

        try:
            res = self.translate_client.translate(text, target_language=self.fixed_tgt)
            translated = html.unescape(res.get('translatedText', text))
        except Exception:
            translated = text

        if is_final:
            self.final_words.extend(self.current_interim.split())
            self.current_interim = ''
            self.final_words.extend(translated.split())
            if len(self.final_words) > self.MAX_WORDS:
                self.final_words = self.final_words[-self.MAX_WORDS:]
        else:
            self.current_interim = translated

        top = ' '.join(self.final_words)
        bottom = self.current_interim
        full = (top + '\n' + bottom).strip()
        lines = textwrap.wrap(full, width=110)
        display = '\n'.join(lines[-2:])
        self.label.config(text=display)
        logging.info(f'Displayed text:\n{display}')

        self.after(self.poll_interval, self._poll_queue)

# ----------------------------------------
# LIVE MIC STREAM
# ----------------------------------------
class MicrophoneStream:
    def __init__(self, rate, chunk, device_index=None):
        self.rate, self.chunk, self.device = rate, chunk, device_index
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        p = pyaudio.PyAudio()
        self.audio_stream = p.open(
            format=pyaudio.paInt16, channels=1, rate=self.rate,
            input=True, input_device_index=self.device,
            frames_per_buffer=self.chunk, stream_callback=self._fill_buffer
        )
        self.closed = False
        return self

    def __exit__(self, *args):
        self.closed = True
        self.audio_stream.stop_stream()
        self.audio_stream.close()

    def _fill_buffer(self, in_data, *_):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        while not self.closed:
            chunk = self._buff.get()
            if chunk is None:
                return
            buff = [chunk]
            while True:
                try:
                    c = self._buff.get(block=False)
                except queue.Empty:
                    break
                buff.append(c)
            yield b''.join(buff)

# ----------------------------------------
# TRANSCRIBER USING V2 API (with chunk splitting + stability filter)
# ----------------------------------------
class Transcriber(threading.Thread):
    def __init__(self, stream_cls, stream_arg, src_language, project_id, recognizer_id):
        super().__init__(daemon=True)
        self.stream_cls = stream_cls
        self.stream_arg = stream_arg
        self.src_language = src_language
        self.project_id = project_id
        self.recognizer_id = recognizer_id
        self.stop_event = threading.Event()
        self.client = SpeechClient()
        self.api_call_count = 0
        self.stt_bytes_sent = 0

    def run(self):
        recognition_config = cloud_speech_types.RecognitionConfig(
            explicit_decoding_config=cloud_speech_types.ExplicitDecodingConfig(
                sample_rate_hertz=RATE,
                encoding=cloud_speech_types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
                audio_channel_count=1,
            ),
            language_codes=[self.src_language],
            model="long",
        )
        streaming_config = cloud_speech_types.StreamingRecognitionConfig(
            config=recognition_config,
            streaming_features=cloud_speech_types.StreamingRecognitionFeatures(
                interim_results=True
            ),
        )
        recognizer_name = (
            f"projects/{self.project_id}/locations/global/recognizers/{self.recognizer_id}"
        )
        config_request = cloud_speech_types.StreamingRecognizeRequest(
            recognizer=recognizer_name,
            streaming_config=streaming_config,
        )

        while not self.stop_event.is_set():
            try:
                self.api_call_count += 1
                logging.info(f"V2 streaming session #{self.api_call_count} using {recognizer_name}")

                ctx = (FileAudioStream(self.stream_arg, RATE, CHUNK)
                       if isinstance(self.stream_arg, str)
                       else MicrophoneStream(RATE, CHUNK, self.stream_arg))
                with ctx as mic:
                    def requests():
                        yield config_request
                        for chunk in mic.generator():
                            for i in range(0, len(chunk), MAX_CHUNK_BYTES):
                                piece = chunk[i:i+MAX_CHUNK_BYTES]
                                self.stt_bytes_sent += len(piece)
                                yield cloud_speech_types.StreamingRecognizeRequest(audio=piece)

                    responses = self.client.streaming_recognize(requests())
                    for resp in responses:
                        if self.stop_event.is_set():
                            break
                        if not resp.results:
                            continue
                        for result in resp.results:
                            if not result.alternatives:
                                continue

                            transcript = result.alternatives[0].transcript.strip()
                            is_final = result.is_final
                            stability = result.stability

                            # skip low-stability interim results
                            if not is_final and stability < STABILITY_THRESHOLD:
                                continue

                            logging.info(
                                f"Transcript: {transcript!r} | final={is_final} | stability={stability:.2f}"
                            )
                            result_queue.put((transcript, is_final))

            except exceptions.OutOfRange:
                logging.warning("Stream limit reached; restarting V2 session")
                time.sleep(0.5)
            except Exception as e:
                logging.error(f"Transcriber V2 error: {e}")
                time.sleep(0.5)

        logging.info(
            f"Total V2 sessions: {self.api_call_count}, bytes sent: {self.stt_bytes_sent}"
        )

    def stop(self):
        self.stop_event.set()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-id', default='stttesting-445210',
                        help='Your GCP project ID')
    parser.add_argument('--recognizer-id', default='_',
                        help='Your Speech-to-Text V2 recognizer resource ID')
    parser.add_argument('--dev-file', help='Path to a mono 16-bit 48 kHz WAV')
    parser.add_argument('--display-interval', type=int, default=DISPLAY_INTERVAL,
                        help='ms between subtitle updates')
    args = parser.parse_args()

    stream_cls = FileAudioStream if args.dev_file else MicrophoneStream
    app = QtWidgets.QApplication(sys.argv)

    while True:
        dlg = SettingsDialog()
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            break
        cfg = dlg.get_settings()
        keyboard.unhook_all()
        keyboard.add_hotkey(cfg['stop_key'], lambda: os._exit(0))

        trans = Transcriber(
            stream_cls,
            args.dev_file or cfg['input_device_index'],
            cfg['src_language'],
            args.project_id,
            args.recognizer_id
        )
        trans.start()

        overlay = SubtitleOverlay(
            cfg['subtitle_color'],
            poll_interval=args.display_interval,
            fixed_tgt=cfg['tgt_language']
        )
        overlay.mainloop()

        logging.info(f"Total translate calls: {overlay.translate_call_count}, chars: {overlay.translate_char_count}")
        trans.stop()
        trans.join()

if __name__ == '__main__':
    main()
