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
from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt
from google.cloud import speech, translate_v2 as translate
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
DISPLAY_INTERVAL = 2000
CHUNK = int(RATE * (DISPLAY_INTERVAL / 1000.0))
SLIDE_HEIGHT = 140

# now holds tuples (text, detected_lang)
result_queue = queue.Queue()

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
        self.resize(400, 300)
        layout = QtWidgets.QVBoxLayout(self)

        # Translation direction (now only for default/fallback)
        layout.addWidget(QtWidgets.QLabel('Default Translation Direction:'))
        self.radio_fr_to_en = QtWidgets.QRadioButton('French → English')
        self.radio_en_to_fr = QtWidgets.QRadioButton('English → French')
        self.radio_fr_to_en.setChecked(True)
        layout.addWidget(self.radio_fr_to_en)
        layout.addWidget(self.radio_en_to_fr)

        # Subtitle color
        self.subtitle_color = '#FFFFFF'
        color_layout = QtWidgets.QHBoxLayout()
        color_layout.addWidget(QtWidgets.QLabel('Subtitle Color:'))
        self.color_preview = QtWidgets.QLabel()
        self.color_preview.setFixedSize(40, 20)
        self.color_preview.setStyleSheet(
            f'background-color: {self.subtitle_color}; border: 1px solid black;'
        )
        color_layout.addWidget(self.color_preview)
        self.color_button = QtWidgets.QPushButton('Choose Color')
        self.color_button.clicked.connect(self.choose_color)
        color_layout.addWidget(self.color_button)
        layout.addLayout(color_layout)

        # Input device
        layout.addWidget(QtWidgets.QLabel('Select Input Device:'))
        self.input_device_combo = QtWidgets.QComboBox()
        self.devices = {}
        p = pyaudio.PyAudio()
        try:
            default_info = p.get_default_input_device_info()
            default_name = default_info.get('name')
        except Exception:
            default_name = None
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get('maxInputChannels', 0) > 0:
                name = info['name']
                self.devices[name] = i
                self.input_device_combo.addItem(name)
                if name == default_name:
                    self.input_device_combo.setCurrentText(name)
        p.terminate()
        layout.addWidget(self.input_device_combo)

        # Global stop key
        layout.addWidget(QtWidgets.QLabel('Global Stop Key:'))
        self.stop_key = 'alt+f11'
        self.stop_key_edit = QtWidgets.QLineEdit(self.stop_key)
        self.stop_key_edit.setReadOnly(True)
        layout.addWidget(self.stop_key_edit)

        # Auto-detect languages
        self.auto_detect_checkbox = QtWidgets.QCheckBox('Auto-detect source language')
        layout.addWidget(self.auto_detect_checkbox)
        self.lang_codes_edit = QtWidgets.QLineEdit('fr-FR,en-US')
        self.lang_codes_edit.setPlaceholderText(
            'Comma-separated BCP-47 codes (up to 4), e.g. fr-FR,en-US'
        )
        self.lang_codes_edit.setEnabled(False)
        layout.addWidget(self.lang_codes_edit)
        # FIX: use toggled(bool) so lang_codes_edit.enable follows the checkbox state
        self.auto_detect_checkbox.toggled.connect(self.lang_codes_edit.setEnabled)

        # OK / Cancel
        btn_layout = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton('OK')
        ok.clicked.connect(self.accept)
        btn_layout.addWidget(ok)
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        btn_layout.addLayout(btn_layout)
        layout.addLayout(btn_layout)

    def choose_color(self):
        color = QtWidgets.QColorDialog.getColor(parent=self)
        if color.isValid():
            self.subtitle_color = color.name()
            self.color_preview.setStyleSheet(
                f'background-color: {self.subtitle_color}; border: 1px solid black;'
            )

    def get_settings(self):
        default_src = 'fr-FR' if self.radio_fr_to_en.isChecked() else 'en-US'
        default_tgt = 'en'    if self.radio_fr_to_en.isChecked() else 'fr-FR'
        codes = [c.strip() for c in self.lang_codes_edit.text().split(',') if c.strip()]
        return {
            'default_src': default_src,
            'default_tgt': default_tgt,
            'subtitle_color': self.subtitle_color,
            'input_device_index': self.devices.get(self.input_device_combo.currentText()),
            'stop_key': self.stop_key,
            'auto_detect': self.auto_detect_checkbox.isChecked(),
            'language_codes': codes
        }

# ----------------------------------------
# OVERLAY WINDOW with AppBar + Dynamic Translation
# ----------------------------------------
class SubtitleOverlay(tk.Tk):
    def __init__(self, subtitle_color, poll_interval, default_tgt):
        super().__init__()
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.config(bg='black')
        try:
            self.wm_attributes('black')
        except tk.TclError:
            pass

        w = self.winfo_screenwidth()
        h = self.winfo_screenheight()
        overlay_height = SLIDE_HEIGHT
        y_pos = h - overlay_height
        self.geometry(f'{w}x{overlay_height}+0+{y_pos}')

        self.after(0, lambda: self._register_appbar(self.winfo_id(), overlay_height))
        self._hook_fullscreen()

        self.label = tk.Label(
            self, text='', font=('Helvetica', 28),
            fg=subtitle_color, bg='black',
            wraplength=w-100, justify='left', anchor='w'
        )
        self.label.place(relx=0, rely=0.5, anchor='w', width=w-100, height=overlay_height)

        self.poll_interval = poll_interval
        self.translate_client = translate.Client()
        self.default_tgt = default_tgt
        self.last_sent = ''
        self.translate_call_count = 0
        self.translate_char_count = 0
        self.after(self.poll_interval, self._poll_queue)

    def _register_appbar(self, hwnd, height):
        ABM_NEW = 0x00000000
        ABM_QUERYPOS = 0x00000002
        ABM_SETPOS = 0x00000003
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
        abd.rc.left = 0
        abd.rc.right = screen_w
        abd.rc.top = screen_h - height
        abd.rc.bottom = screen_h
        ctypes.windll.shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))

    def _hook_fullscreen(self):
        def shrink_cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                rect = win32gui.GetWindowRect(hwnd)
                mon_handle = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
                mon = win32api.GetMonitorInfo(mon_handle).get('Monitor')
                if rect == mon:
                    left, top, right, bottom = mon
                    new_h = (bottom - top) - SLIDE_HEIGHT
                    win32gui.SetWindowPos(hwnd, None, left, top, right-left, new_h, win32con.SWP_NOZORDER)
            except Exception:
                pass
        win32gui.EnumWindows(shrink_cb, None)
        self.after(1000, self._hook_fullscreen)

    def _poll_queue(self):
        latest = None
        detected = None
        while True:
            try:
                raw, lang = result_queue.get_nowait()
                latest, detected = raw, lang
            except queue.Empty:
                break

        if not latest:
            self.after(self.poll_interval, self._poll_queue)
            return

        # compute delta
        if latest.startswith(self.last_sent):
            delta = latest[len(self.last_sent):].strip()
        else:
            delta = latest
        if not delta:
            self.after(self.poll_interval, self._poll_queue)
            return

        # pick target: flip french<->english
        tgt = self.default_tgt
        if detected.startswith('fr'):
            tgt = 'en'
        elif detected.startswith('en'):
            tgt = 'fr-FR'

        self.translate_call_count += 1
        self.translate_char_count += len(delta)
        logging.info(f'Translate API call #{self.translate_call_count}, chars {len(delta)}, detected={detected}')

        try:
            res = self.translate_client.translate(delta, target_language=tgt)
            translated = html.unescape(res.get('translatedText', delta))
        except Exception:
            translated = delta

        full = (self.label.cget('text') + ' ' + translated).strip()
        lines = textwrap.wrap(full, width=110)
        self.label.config(text='\n'.join(lines[-2:]))

        self.last_sent = latest
        self.after(self.poll_interval, self._poll_queue)

# ----------------------------------------
# LIVE MIC STREAM and Transcriber
# ----------------------------------------
class MicrophoneStream:
    def __init__(self, rate, chunk, device_index=None):
        self.rate = rate
        self.chunk = chunk
        self.device = device_index
        self._buff = queue.Queue()
        self.closed = True

    def __enter__(self):
        p = pyaudio.PyAudio()
        self.audio_stream = p.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.rate,
            input=True,
            input_device_index=self.device,
            frames_per_buffer=self.chunk,
            stream_callback=self._fill_buffer
        )
        self.closed = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.closed = True
        self.audio_stream.stop_stream()
        self.audio_stream.close()

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
                data.append(c)
            yield b''.join(data)

class Transcriber(threading.Thread):
    def __init__(self, default_src, default_tgt, stream_cls, stream_arg,
                 auto_detect=False, language_codes=None):
        super().__init__(daemon=True)
        self.default_src = default_src
        self.default_tgt = default_tgt
        self.stream_cls = stream_cls
        self.stream_arg = stream_arg
        self.auto_detect = auto_detect
        self.language_codes = language_codes or []
        self.stop_event = threading.Event()
        self.speech = speech.SpeechClient()
        self.api_call_count = 0
        self.stt_bytes_sent = 0

    def run(self):
        # build config
        if self.auto_detect and self.language_codes:
            primary = self.language_codes[0]
            alternatives = self.language_codes[1:]
            cfg = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=RATE,
                language_code=primary,
                alternative_language_codes=alternatives,
                enable_automatic_punctuation=True,
                enable_word_confidence=True,
                model='phone_call',
                use_enhanced=True
            )
            logging.info(f'Auto-detect enabled; langs={self.language_codes}')
        else:
            cfg = speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=RATE,
                language_code=self.default_src,
                enable_automatic_punctuation=True,
                enable_word_confidence=True,
                model='phone_call',
                use_enhanced=True
            )

        stream_cfg = speech.StreamingRecognitionConfig(config=cfg, interim_results=True)

        while not self.stop_event.is_set():
            try:
                self.api_call_count += 1
                logging.info(f'Streaming session #{self.api_call_count} started')

                mic_ctx = (
                    FileAudioStream(self.stream_arg, RATE, CHUNK)
                    if isinstance(self.stream_arg, str)
                    else MicrophoneStream(RATE, CHUNK, self.stream_arg)
                )

                with mic_ctx as mic:
                    def request_gen():
                        for chunk in mic.generator():
                            self.stt_bytes_sent += len(chunk)
                            yield speech.StreamingRecognizeRequest(audio_content=chunk)

                    for resp in self.speech.streaming_recognize(stream_cfg, request_gen()):
                        if self.stop_event.is_set():
                            break
                        if not resp.results or not resp.results[0].alternatives:
                            continue
                        text = resp.results[0].alternatives[0].transcript.strip()
                        lang = resp.results[0].language_code
                        if text:
                            result_queue.put((text, lang))
                            prefix = 'Final' if resp.results[0].is_final else 'Interim'
                            logging.debug(f'{prefix} ({lang}): {text}')

            except exceptions.OutOfRange:
                logging.warning('Stream duration exceeded; restarting')
                time.sleep(0.5)
                continue
            except Exception as e:
                logging.error(f'Transcriber error: {e}')
                time.sleep(0.5)
                continue

            if isinstance(self.stream_arg, str):
                logging.info('Dev-file complete; stopping Transcriber.')
                break

        logging.info(f'Total streaming sessions: {self.api_call_count}')
        logging.info(f'Total STT bytes sent: {self.stt_bytes_sent}')
        logging.info('Transcriber stopped.')

    def stop(self):
        self.stop_event.set()

# ----------------------------------------
def main():
    parser = argparse.ArgumentParser()
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
            cfg['default_src'], cfg['default_tgt'],
            stream_cls, args.dev_file or cfg['input_device_index'],
            auto_detect=cfg['auto_detect'],
            language_codes=cfg['language_codes']
        )
        trans.start()

        overlay = SubtitleOverlay(
            cfg['subtitle_color'],
            poll_interval=args.display_interval,
            default_tgt=cfg['default_tgt']
        )
        overlay.mainloop()

        logging.info(f'Total translate API calls: {overlay.translate_call_count}')
        logging.info(f'Total translation characters sent: {overlay.translate_char_count}')

        trans.stop()
        trans.join()

if __name__ == '__main__':
    main()
