# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# ...

import argparse
import queue
import re
import sys
import time

from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech as cloud_speech_types
import pyaudio

# Audio recording parameters
STREAMING_LIMIT = 240000  # 4 minutes
SAMPLE_RATE = 16000
CHUNK_SIZE = int(SAMPLE_RATE / 10)  # 100ms

RED = "\033[0;31m"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"


def get_current_time() -> int:
    return int(round(time.time() * 1000))


class ResumableMicrophoneStream:
    """Opens a recording stream as a generator yielding single‐chunk audio frames."""

    def __init__(self, rate: int, chunk_size: int) -> None:
        self._rate = rate
        self.chunk_size = chunk_size
        self._num_channels = 1
        self._buff = queue.Queue()
        self.closed = True
        self.start_time = get_current_time()
        self.restart_counter = 0
        self.audio_input = []
        self.last_audio_input = []
        self.result_end_time = 0
        self.is_final_end_time = 0
        self.final_request_end_time = 0
        self.bridging_offset = 0
        self.last_transcript_was_final = False
        self.new_stream = True

        self._audio_interface = pyaudio.PyAudio()
        self._audio_stream = self._audio_interface.open(
            format=pyaudio.paInt16,
            channels=self._num_channels,
            rate=self._rate,
            input=True,
            frames_per_buffer=self.chunk_size,
            stream_callback=self._fill_buffer,
        )

    def __enter__(self) -> "ResumableMicrophoneStream":
        self.closed = False
        return self

    def __exit__(self, type, value, traceback) -> None:
        self._audio_stream.stop_stream()
        self._audio_stream.close()
        self.closed = True
        self._buff.put(None)
        self._audio_interface.terminate()

    def _fill_buffer(self, in_data, *args, **kwargs):
        self._buff.put(in_data)
        return None, pyaudio.paContinue

    def generator(self):
        """Yield single‐chunk byte frames, including any bridged-from-last-stream chunks."""
        while not self.closed:
            # replay buffered last_audio_input chunks (bridging) one‐by‐one
            if self.new_stream and self.last_audio_input:
                chunk_time = STREAMING_LIMIT / len(self.last_audio_input)
                if chunk_time and self.bridging_offset < self.final_request_end_time:
                    chunks_from_ms = round(
                        (self.final_request_end_time - self.bridging_offset) / chunk_time
                    )
                    self.bridging_offset = round(
                        (len(self.last_audio_input) - chunks_from_ms) * chunk_time
                    )
                    for chunk in self.last_audio_input[chunks_from_ms:]:
                        yield chunk
                self.new_stream = False

            # now yield live audio frames as they arrive
            chunk = self._buff.get()
            if chunk is None:
                return
            self.audio_input.append(chunk)
            yield chunk

            # flush out any remaining buffered frames
            while True:
                try:
                    chunk = self._buff.get(block=False)
                    if chunk is None:
                        return
                    self.audio_input.append(chunk)
                    yield chunk
                except queue.Empty:
                    break


def listen_print_loop(responses, stream: ResumableMicrophoneStream) -> None:
    for response in responses:
        if get_current_time() - stream.start_time > STREAMING_LIMIT:
            stream.start_time = get_current_time()
            break

        if not response.results:
            continue
        result = response.results[0]
        if not result.alternatives:
            continue

        transcript = result.alternatives[0].transcript
        result_seconds = getattr(result.result_end_offset, "seconds", 0)
        result_micros = getattr(result.result_end_offset, "microseconds", 0)
        stream.result_end_time = int((result_seconds * 1000) + (result_micros / 1000))
        corrected_time = (
            stream.result_end_time
            - stream.bridging_offset
            + (STREAMING_LIMIT * stream.restart_counter)
        )

        if result.is_final:
            sys.stdout.write(GREEN + "\033[K")
            sys.stdout.write(f"{corrected_time}: {transcript}\n")
            stream.is_final_end_time = stream.result_end_time
            stream.last_transcript_was_final = True
            if re.search(r"\b(exit|quit)\b", transcript, re.I):
                sys.stdout.write(YELLOW + "Exiting...\n")
                stream.closed = True
                break
        else:
            sys.stdout.write(RED + "\033[K")
            sys.stdout.write(f"{corrected_time}: {transcript}\r")
            stream.last_transcript_was_final = False


def main(project_id: str) -> None:
    client = SpeechClient()
    recognition_config = cloud_speech_types.RecognitionConfig(
        explicit_decoding_config=cloud_speech_types.ExplicitDecodingConfig(
            sample_rate_hertz=SAMPLE_RATE,
            encoding=cloud_speech_types.ExplicitDecodingConfig.AudioEncoding.LINEAR16,
            audio_channel_count=1,
        ),
        language_codes=["fr-FR"],
        model="long",
    )
    streaming_config = cloud_speech_types.StreamingRecognitionConfig(
        config=recognition_config,
        streaming_features=cloud_speech_types.StreamingRecognitionFeatures(
            interim_results=True
        ),
    )
    config_request = cloud_speech_types.StreamingRecognizeRequest(
        recognizer=f"projects/{project_id}/locations/global/recognizers/_",
        streaming_config=streaming_config,
    )

    def requests(config, audio_gen):
        yield config
        for chunk in audio_gen:
            yield cloud_speech_types.StreamingRecognizeRequest(audio=chunk)

    mic_manager = ResumableMicrophoneStream(SAMPLE_RATE, CHUNK_SIZE)
    print(mic_manager.chunk_size)
    sys.stdout.write(YELLOW + '\nListening, say "Quit" or "Exit" to stop.\n\n')
    sys.stdout.write("End (ms)       Transcript Results/Status\n")
    sys.stdout.write("=====================================================\n")

    with mic_manager as stream:
        while not stream.closed:
            sys.stdout.write(YELLOW + f"\n{STREAMING_LIMIT * stream.restart_counter}: NEW REQUEST\n")
            stream.audio_input = []
            audio_generator = stream.generator()
            responses_iterator = client.streaming_recognize(
                requests=requests(config_request, audio_generator)
            )
            listen_print_loop(responses_iterator, stream)

            if stream.result_end_time > 0:
                stream.final_request_end_time = stream.is_final_end_time
            stream.last_audio_input = stream.audio_input
            stream.result_end_time = 0
            stream.restart_counter += 1
            stream.new_stream = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    main(project_id="stttesting-445210")
