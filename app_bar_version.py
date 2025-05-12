#!/usr/bin/env python3
import sys
import os
import queue
import threading
import logging
import html
import wave
import argparse
import re
import time
import textwrap
import ctypes
from ctypes import wintypes

import pyaudio
import tkinter as tk
import keyboard
import win32process
from PyQt5 import QtWidgets
from google.cloud import speech, translate_v2 as translate
from google.api_core import exceptions

# Windows-specific imports for window enumeration and resizing
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
CHUNK = RATE // 2
DISPLAY_INTERVAL = 3500
SLIDE_HEIGHT = 140  # reserved bottom height

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
# SETTINGS DIALOG (PyQt5) with Window Picker
# ----------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Settings')
        self.setModal(True)
        self.resize(400, 350)
        layout = QtWidgets.QVBoxLayout(self)

        # Translation direction
        layout.addWidget(QtWidgets.QLabel('Select Translation Direction:'))
        self.radio_fr_to_en = QtWidgets.QRadioButton('French to English')
        self.radio_en_to_fr = QtWidgets.QRadioButton('English to French')
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

        # Slideshow window picker
        layout.addWidget(QtWidgets.QLabel('Select Slideshow Window:'))
        self.window_combo = QtWidgets.QComboBox()
        self.window_map = {}
        self._populate_windows()
        layout.addWidget(self.window_combo)
        refresh_btn = QtWidgets.QPushButton('Refresh List')
        refresh_btn.clicked.connect(self._refresh_windows)
        layout.addWidget(refresh_btn)

        # OK / Cancel
        btn_layout = QtWidgets.QHBoxLayout()
        ok = QtWidgets.QPushButton('OK')
        ok.clicked.connect(self.accept)
        btn_layout.addWidget(ok)
        cancel = QtWidgets.QPushButton('Cancel')
        cancel.clicked.connect(self.reject)
        btn_layout.addWidget(cancel)
        layout.addLayout(btn_layout)

    def choose_color(self):
        color = QtWidgets.QColorDialog.getColor(parent=self)
        if color.isValid():
            self.subtitle_color = color.name()
            self.color_preview.setStyleSheet(
                f'background-color: {self.subtitle_color}; border: 1px solid black;'
            )

    def _populate_windows(self):
        self.window_combo.clear()
        self.window_map.clear()

        # 1) Collect all top‐level window handles into a list
        window_list = []

        def enum_cb(hwnd, extra):
            window_list.append(hwnd)

        win32gui.EnumWindows(enum_cb, None)

        # 2) Iterate and add only visible ones, with rich metadata
        for hwnd in window_list:
            if not win32gui.IsWindowVisible(hwnd):
                continue

            # Title placeholder if empty
            title = win32gui.GetWindowText(hwnd) or '<No Title>'
            # Window class
            cls = win32gui.GetClassName(hwnd)
            # Process ID
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            # Geometry: left, top, right, bottom
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            width, height = right - left, bottom - top

            display = (
                f"{hwnd:#010x} – {title} "
                f"[class={cls} pid={pid} "
                f"rect={width}×{height} @{left},{top}]"
            )

            self.window_map[display] = hwnd
            self.window_combo.addItem(display)


    def _refresh_windows(self):
        self._populate_windows()

    def get_settings(self):
        direction = ('fr-FR', 'en') if self.radio_fr_to_en.isChecked() else ('en-US', 'fr-FR')
        selected = self.window_combo.currentText()
        hwnd = self.window_map.get(selected)
        return {
            'source_lang': direction[0],
            'target_lang': direction[1],
            'subtitle_color': self.subtitle_color,
            'input_device_index': self.devices.get(self.input_device_combo.currentText()),
            'stop_key': self.stop_key,
            'slideshow_hwnd': hwnd,
        }

# ----------------------------------------
# OVERLAY WINDOW + AppBar + Slideshow Hook
# ----------------------------------------
class SubtitleOverlay(tk.Tk):
    def __init__(self, subtitle_color, poll_interval, target_lang, slideshow_hwnd):
        super().__init__()
        self.overrideredirect(True)
        self.attributes('-topmost', True)
        self.config(bg='black')
        try:
            self.wm_attributes('-transparentcolor', 'black')
        except tk.TclError:
            pass

        w = self.winfo_screenwidth()
        h = self.winfo_screenheight()
        y_pos = h - SLIDE_HEIGHT
        self.geometry(f"{w}x{SLIDE_HEIGHT}+0+{y_pos}")

        self.label = tk.Label(
            self, text='', font=('Helvetica', 28),
            fg=subtitle_color, bg='black',
            wraplength=w-100, justify='left', anchor='w'
        )
        self.label.place(relx=0, rely=0.5, anchor='w', width=w-100, height=SLIDE_HEIGHT)

        # Register AppBar on Windows
        self.after(0, lambda: self._register_appbar(self.winfo_id(), SLIDE_HEIGHT))
        # Store selected slideshow handle
        self.slideshow_hwnd = slideshow_hwnd
        # Begin periodic hook
        self.after(1000, self._hook_slideshow)

        self.poll_interval = poll_interval
        self.translate_client = translate.Client()
        self.target_lang = target_lang
        self.lines = []
        self.after(self.poll_interval, self._poll_queue)

    def _register_appbar(self, hwnd, height):
        ABM_NEW      = 0x00000000
        ABM_QUERYPOS = 0x00000002
        ABM_SETPOS   = 0x00000003
        ABE_BOTTOM   = 3

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
        abd.hWnd   = hwnd
        abd.uEdge  = ABE_BOTTOM

        ctypes.windll.shell32.SHAppBarMessage(ABM_NEW, ctypes.byref(abd))

        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        abd.rc.left   = 0
        abd.rc.right  = screen_w
        abd.rc.top    = screen_h - height
        abd.rc.bottom = screen_h
        ctypes.windll.shell32.SHAppBarMessage(ABM_QUERYPOS, ctypes.byref(abd))
        ctypes.windll.shell32.SHAppBarMessage(ABM_SETPOS, ctypes.byref(abd))

        ctypes.windll.user32.SetWindowPos(
            hwnd, -1,
            abd.rc.left, abd.rc.top,
            abd.rc.right - abd.rc.left,
            abd.rc.bottom - abd.rc.top,
            0x0004  # SWP_NOZORDER
        )

    def _hook_slideshow(self):
        if self.slideshow_hwnd and win32gui.IsWindow(self.slideshow_hwnd):
            # get monitor bounds
            mi = win32api.MonitorFromWindow(self.slideshow_hwnd)
            mon = win32api.GetMonitorInfo(mi)['Monitor']
            left, top, right, bottom = mon
            # shrink height by reserved strip
            new_h = (bottom - top) - SLIDE_HEIGHT
            win32gui.SetWindowPos(
                self.slideshow_hwnd, None,
                left, top,
                right - left, new_h,
                win32con.SWP_NOZORDER
            )
        self.after(1000, self._hook_slideshow)

    def _poll_queue(self):
        latest = None
        while True:
            try:
                raw = result_queue.get_nowait()
            except queue.Empty:
                break
            if raw is not None:
                latest = raw

        if latest:
            parts = re.split(r'(?<=[.?!])\s+', latest)
            for sentence in parts:
                if not sentence:
                    continue
                try:
                    res = self.translate_client.translate(sentence,
                                                          target_language=self.target_lang)
                    translated = html.unescape(res.get('translatedText', sentence))
                except Exception:
                    translated = sentence
                new_lines = textwrap.wrap(translated, width=110)
                self.lines = new_lines[-2:] if len(new_lines) > 2 else new_lines
                display_text = '\n'.join(self.lines)
                self.label.config(text=display_text)
                logging.info(f'Subtitle:\n{display_text}')
                break

        self.after(self.poll_interval, self._poll_queue)

# ----------------------------------------
# LIVE MIC STREAM
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

# ----------------------------------------
# TRANSCRIBER THREAD
# ----------------------------------------
class Transcriber(threading.Thread):
    def __init__(self, src, tgt, stream_cls, stream_arg):
        super().__init__(daemon=True)
        self.src = src
        self.tgt = tgt
        self.stream_cls = stream_cls
        self.stream_arg = stream_arg
        self.stop_event = threading.Event()
        self.speech = speech.SpeechClient()

    def run(self):
        cfg = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=RATE,
            language_code=self.src,
            enable_automatic_punctuation=True,
            enable_word_confidence=True,
            model='phone_call',
            use_enhanced=True
        )
        stream_cfg = speech.StreamingRecognitionConfig(config=cfg, interim_results=True)

        while not self.stop_event.is_set():
            try:
                logging.info('Starting new speech stream')
                mic_ctx = (
                    FileAudioStream(self.stream_arg, RATE, CHUNK)
                    if isinstance(self.stream_arg, str)
                    else MicrophoneStream(RATE, CHUNK, self.stream_arg)
                )
                with mic_ctx as mic:
                    requests = (
                        speech.StreamingRecognizeRequest(audio_content=chunk)
                        for chunk in mic.generator()
                    )
                    for resp in self.speech.streaming_recognize(stream_cfg, requests):
                        if self.stop_event.is_set():
                            break
                        if not resp.results or not resp.results[0].alternatives:
                            continue
                        text = resp.results[0].alternatives[0].transcript.strip()
                        if text:
                            result_queue.put(text)
                            prefix = 'Final' if resp.results[0].is_final else 'Interim'
                            logging.info(f'{prefix}: {text}')

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

        stream_arg = args.dev_file or cfg['input_device_index']
        trans = Transcriber(cfg['source_lang'], cfg['target_lang'], stream_cls, stream_arg)
        trans.start()

        SubtitleOverlay(cfg['subtitle_color'], poll_interval=args.display_interval,
                        target_lang=cfg['target_lang'], slideshow_hwnd=cfg['slideshow_hwnd']).mainloop()

        trans.stop()
        trans.join()

if __name__ == '__main__':
    main()
