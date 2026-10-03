import copy
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from types import SimpleNamespace
import wave

from lumen.captions import (
    CaptionError, normalize_captions, parse_srt, parse_vtt, load_captions,
    retime_captions, write_srt, write_vtt,
    Transcriber, TranscriptionCancelled, TranscriptionError, discover_transcriber,
)
from lumen.speech import build_audio_command
from lumen.speech_worker import segment_captions


def cue(start=1., end=3., text="Hello", identifier="one", **extra):
    return {"id": identifier, "start": start, "end": end, "text": text, **extra}


class CaptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lumen-captions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_srt_bom_crlf_unicode_and_multiline(self):
        text = "\ufeff1\r\n00:00:01,250 --> 00:00:03,500\r\nBonjour 世界 👋\r\nCafé &amp; tea\r\n"
        result = parse_srt(text)
        self.assertEqual((result[0]["start"], result[0]["end"]), (1.25, 3.5))
        self.assertEqual(result[0]["text"], "Bonjour 世界 👋\nCafé & tea")
        self.assertEqual(parse_srt(text), result)

    def test_vtt_metadata_notes_and_cue_settings(self):
        text = "WEBVTT Example\nKind: captions\nLanguage: en\n\nNOTE metadata --> ignored\nmore notes\n\nSTYLE\n::cue { color: lime; }\n\nchapter-one\n00:01.500 --> 00:04.000 align:start position:10%\n<v Alice><b>Hello</b> &lt;b&gt;literal&lt;/b&gt;\n<c.green>World</c> <00:02.000>again</v>\n"
        result = parse_vtt(text)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "Hello <b>literal</b>\nWorld again")
        self.assertEqual(result[0]["start"], 1.5)

    def test_srt_roundtrip_literal_delimiters_markup_and_blank_lines(self):
        captions = [cue(text="Use x < y & z > 2\n\nLiteral <b>bold?</b> --> {\\an8}\n世界")]
        path = write_srt(captions, self.root / "captions.srt")
        text = path.read_text()
        self.assertIn("--&gt;", text)
        self.assertIn("&lt;b&gt;", text)
        self.assertEqual(load_captions(path)[0]["text"], captions[0]["text"])

    def test_vtt_roundtrip_unicode_and_literal_arrow(self):
        path = write_vtt([cue(text="Привет → --> & <tag>")], self.root / "世界.vtt")
        self.assertTrue(path.read_text().startswith("WEBVTT\n\n"))
        self.assertEqual(load_captions(path)[0]["text"], "Привет → --> & <tag>")

    def test_common_srt_font_markup_becomes_plain_text(self):
        result = parse_srt('1\n00:00:01,000 --> 00:00:02,000\n<font color="#fff"><i>Text</i></font>\n')
        self.assertEqual(result[0]["text"], "Text")

    def test_sort_keeps_overlaps_and_does_not_mutate_input(self):
        captions = [cue(2, 4, "second", "b"), cue(1, 3, "first", "a")]
        original = copy.deepcopy(captions)
        result = normalize_captions(captions)
        self.assertEqual([c["id"] for c in result], ["a", "b"])
        self.assertEqual(captions, original)

    def test_invalid_ranges_types_and_duplicate_ids(self):
        bad = [cue(-1, 2), cue(1, 1), cue(3, 2), cue(float("nan"), 3),
               cue(1, float("inf")), cue(True, 3), cue(text=""), cue(text="bad\x00text"),
               cue(enabled="false"), cue(text=["no"]), cue(text="x"*10_001), cue(identifier="x"*201)]
        for item in bad:
            with self.subTest(item=item), self.assertRaises(CaptionError):
                normalize_captions([item])
        with self.assertRaises(CaptionError):
            normalize_captions([cue(), cue()])
        with self.assertRaises(CaptionError):
            normalize_captions([cue()], duration=2)

    def test_caption_count_matches_renderer_limit(self):
        captions = [cue(index, index+1, "x", str(index)) for index in range(10_000)]
        self.assertEqual(len(normalize_captions(captions)), 10_000)
        with self.assertRaisesRegex(CaptionError, "10,000"):
            normalize_captions(captions + [cue(10_000, 10_001, "extra", "extra")])

    def test_invalid_subtitle_blocks_do_not_silently_disappear(self):
        for text in ["1\nnot a time\nhello", "1\n00:60:00,000 --> 01:01:00,000\nhello",
                     "1\n00:00:04,000 --> 00:00:03,000\nhello", "1\n00:00:01,000 --> 00:00:02,000\n"]:
            with self.subTest(text=text), self.assertRaises(CaptionError):
                parse_srt(text)
        with self.assertRaises(CaptionError):
            parse_vtt("Missing header\n")

    def test_retime_clips_boundaries_and_preserves_overlaps(self):
        source = [cue(0, 2, "outside", "a"), cue(1, 4, "left", "b"),
                  cue(3, 8, "right", "c"), cue(6, 9, "after", "d")]
        result = retime_captions(source, trim_start=2, trim_end=6, speed=2)
        self.assertEqual([(c["id"], c["start"], c["end"]) for c in result], [("b", 0., 1.), ("c", .5, 2.)])
        self.assertEqual(source[1]["start"], 1)

    def test_retime_disabled_and_invalid_options(self):
        self.assertEqual(retime_captions([cue(enabled=False)]), [])
        self.assertEqual(len(retime_captions([cue(enabled=False)], include_disabled=True)), 1)
        for settings in [{"speed": 0}, {"speed": float("nan")}, {"trim_start": -1},
                         {"trim_start": 2, "trim_end": 2}, {"speed": True}]:
            with self.subTest(settings=settings), self.assertRaises(CaptionError):
                retime_captions([cue()], **settings)

    def test_writer_applies_trim_speed_and_millisecond_rounding(self):
        path = write_srt([cue(10.0004, 12.0004)], self.root / "retimed.srt", trim_start=10, trim_end=11, speed=.5)
        result = load_captions(path)[0]
        self.assertEqual(result["start"], .001)
        self.assertEqual(result["end"], 2)

    def test_existing_sidecar_is_preserved_and_no_partial_file_remains(self):
        path = self.root / "existing.srt"
        path.write_text("keep this")
        with self.assertRaises(FileExistsError):
            write_srt([cue()], path)
        self.assertEqual(path.read_text(), "keep this")
        self.assertEqual(list(self.root.iterdir()), [path])
        with self.assertRaises(CaptionError):
            write_srt([cue(end=0)], path, overwrite=True)
        self.assertEqual(path.read_text(), "keep this")

    def test_empty_sidecars_and_utf8_requirement(self):
        self.assertEqual(parse_srt(""), [])
        self.assertEqual(parse_vtt("WEBVTT\n\n"), [])
        self.assertEqual(load_captions(write_vtt([], self.root / "empty.vtt")), [])
        bad = self.root / "legacy.srt"
        bad.write_bytes(b"\xffnot utf8")
        with self.assertRaisesRegex(CaptionError, "UTF-8"):
            load_captions(bad)


class SpeechProcessTests(unittest.TestCase):
    def test_explicit_whisper_cpp_overrides_prepared_python_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            model=Path(temporary)/"ggml.bin"
            model.write_bytes(b"test model path")
            with mock.patch.dict(os.environ,{"LUMEN_WHISPER_CLI":sys.executable,"LUMEN_WHISPER_MODEL":str(model)}):
                runtime=discover_transcriber()
            self.assertTrue(runtime["available"])
            self.assertEqual(runtime["backend"],"whisper.cpp")

    def test_cancellation_reaps_an_uncooperative_owned_process(self):
        worker = Transcriber()
        ready = threading.Event()
        errors = []
        def run():
            try:
                worker._run([sys.executable, "-u", "-c", "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print('ready',flush=True); time.sleep(30)"], on_line=lambda line: ready.set())
            except Exception as exc:
                errors.append(exc)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(ready.wait(3))
        started = time.monotonic()
        worker.cancel()
        self.assertLess(time.monotonic() - started, .5)
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], TranscriptionCancelled)
        self.assertIsNone(worker._process)

    def test_process_error_preserves_actionable_stderr(self):
        with self.assertRaisesRegex(TranscriptionError, "Missing local model"):
            Transcriber()._run([sys.executable, "-c", "import sys; print('Missing local model',file=sys.stderr); sys.exit(3)"])

    def test_missing_runtime_does_not_start_audio_extraction(self):
        with mock.patch("lumen.speech.build_audio_command") as build:
            with self.assertRaisesRegex(TranscriptionError, "Install optional runtime"):
                Transcriber({"available":False,"reason":"Install optional runtime"}).transcribe({})
            build.assert_not_called()


class SpeechGroupingTests(unittest.TestCase):
    def test_aligned_words_split_without_loss_or_invented_times(self):
        tokens = " This is a longer explanation about recording your desktop and making clear captions for everyone.".split(" ")[1:]
        words = [SimpleNamespace(start=index*.45, end=index*.45+.4, word=" "+token) for index,token in enumerate(tokens)]
        segment = SimpleNamespace(start=0,end=words[-1].end,text="".join(w.word for w in words),words=words)
        captions = segment_captions(segment)
        self.assertGreater(len(captions), 2)
        self.assertEqual(" ".join(c["text"] for c in captions).split(), tokens)
        for caption in captions:
            self.assertIn(caption["start"], [w.start for w in words])
            self.assertIn(caption["end"], [w.end for w in words])
            self.assertLessEqual(len(caption["text"]), 42)
            self.assertLessEqual(caption["end"]-caption["start"], 3)

    def test_natural_punctuation_boundary_is_preferred(self):
        words = [SimpleNamespace(start=0,end=.5,word="Hello"),
                 SimpleNamespace(start=.6,end=1.5,word=" everyone."),
                 SimpleNamespace(start=1.6,end=2,word=" Welcome"),
                 SimpleNamespace(start=2.1,end=2.5,word=" back.")]
        segment=SimpleNamespace(start=0,end=2.5,text="Hello everyone. Welcome back.",words=words)
        result=segment_captions(segment)
        self.assertEqual([c["text"] for c in result], ["Hello everyone.","Welcome back."])
        self.assertEqual((result[0]["end"], result[1]["start"]), (1.5,1.6))

    def test_no_word_alignment_falls_back_to_original_segment(self):
        segment=SimpleNamespace(start=1.23,end=5.67,text=" Original model segment. ",words=None)
        self.assertEqual(segment_captions(segment), [{"start":1.23,"end":5.67,"text":"Original model segment."}])
        segment.words=[SimpleNamespace(start=float("nan"),end=3,word="Original")]
        self.assertEqual(segment_captions(segment)[0]["start"], 1.23)

    def test_non_latin_words_preserve_the_models_spacing(self):
        text="これは字幕のテストです。"
        words=[SimpleNamespace(start=i*.3,end=i*.3+.25,word=char) for i,char in enumerate(text)]
        segment=SimpleNamespace(start=0,end=words[-1].end,text=text,words=words)
        result=segment_captions(segment,max_chars=5)
        self.assertEqual("".join(c["text"] for c in result), text)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class CaptionAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="lumen-caption-audio-")
        cls.root = Path(cls.temporary.name)
        source = cls.root / "source.mkv"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                        "-f", "lavfi", "-i", "color=black:size=32x32:rate=10:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=48000:duration=1",
                        "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "ffv1", "-c:a", "pcm_s16le", str(source)],
                       check=True, capture_output=True)
        cls.project = {"path":str(cls.root), "source":"source.mkv", "audio_tracks":{"desktop":0,"mic":1}}

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_desktop_and_mic_extraction_select_different_actual_audio(self):
        import array
        for mode, expected in (("desktop", 440), ("mic", 880)):
            output = self.root / (mode + ".wav")
            command, duration = build_audio_command(self.project, output, mode)
            subprocess.run(command, check=True, capture_output=True)
            with wave.open(str(output)) as audio:
                self.assertEqual((audio.getnchannels(), audio.getframerate()), (1, 16000))
                samples = array.array("h", audio.readframes(audio.getnframes()))
            crossings = sum(a <= 0 < b for a,b in zip(samples, samples[1:]))
            frequency = crossings / (len(samples) / 16000)
            self.assertAlmostEqual(frequency, expected, delta=3)
            self.assertAlmostEqual(duration, 1, delta=.05)

    def test_absent_selected_track_fails_instead_of_using_other_track(self):
        project = {**self.project, "audio_tracks":{"desktop":0}}
        with self.assertRaisesRegex(TranscriptionError, "no mic audio"):
            build_audio_command(project, self.root / "missing.wav", "mic")
        with self.assertRaises(TranscriptionError):
            build_audio_command(project, self.root / "silent.wav", "none")


if __name__ == "__main__":
    unittest.main()
