"""Mic toggles own only private modules and acknowledge the actual route."""
import copy
import io
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from lumen.microphone import LiveMicrophone, MicrophoneError
from lumen.microphone_worker import AudioError, AudioSession


class FakePulse:
    def __init__(self):
        self.calls = []
        self.modules = {}
        self.sources = [{"name": "test.monitor", "index": 10, "owner_module": 80},
                        {"name": "other.monitor", "index": 11, "owner_module": 81}]
        self.sinks = []
        self.outputs = []
        self.inputs = []
        self.default = "test.monitor"
        self.next_module = 500
        self.fail_unload = False

    def __call__(self, *args):
        self.calls.append(args)
        if args[:3] == ("-f", "json", "list"):
            return json.dumps({"sources": self.sources, "sinks": self.sinks,
                               "source-outputs": self.outputs, "sink-inputs": self.inputs}[args[3]])
        if args == ("get-default-source",):
            return self.default
        if args == ("list", "short", "modules"):
            return "\n".join(f"{i}\t{kind}\t{' '.join(options)}\t"
                             for i, (kind, options) in self.modules.items())
        if args[0] == "load-module":
            index = self.next_module
            self.next_module += 1
            kind, options = args[1], args[2:]
            self.modules[index] = (kind, options)
            values = dict(o.split("=", 1) for o in options)
            if kind == "module-null-sink":
                self.sources.append({"name": values["sink_name"] + ".monitor", "index": 30,
                                     "owner_module": index})
                self.sinks.append({"name": values["sink_name"], "index": 31, "owner_module": index})
            elif kind == "module-loopback":
                source = next(s["index"] for s in self.sources if s["name"] == values["source"])
                tag = values["source_output_properties"].split("=", 1)[1]
                self.outputs.append({"index": 52, "source": source, "owner_module": index,
                                     "properties": {"application.name": tag}})
                self.inputs.append({"index": 53, "sink": 31, "owner_module": index,
                                    "properties": {"application.name": tag}})
            return str(index)
        if args[0] == "unload-module":
            if self.fail_unload:
                raise AudioError("Permission denied")
            index = int(args[1])
            self.modules.pop(index, None)
            self.sources = [s for s in self.sources if s.get("owner_module") != index]
            self.sinks = [s for s in self.sinks if s.get("owner_module") != index]
            self.outputs = [s for s in self.outputs if s.get("owner_module") != index]
            self.inputs = [s for s in self.inputs if s.get("owner_module") != index]
            return ""
        raise AssertionError(args)

    def reader(self, pid=123, source=30, index=51):
        self.outputs.append({"index": index, "source": source,
                             "properties": {"application.process.id": str(pid)}})


class MicrophoneSessionTests(TestCase):
    def setUp(self):
        self.pulse = FakePulse()
        self.addCleanup(patch.stopall)
        patch("lumen.microphone_worker._run", side_effect=self.pulse).start()
        self.identity = patch("lumen.microphone_worker._pid_identity", return_value="identity").start()
        self.session = AudioSession("test.monitor")

    def start(self, enabled=False):
        self.session.initial_enabled = enabled
        source = self.session.start()
        self.pulse.reader()
        self.session.bind(123)
        return source

    def test_start_and_bind_off_create_only_silence_and_do_not_resolve_default(self):
        self.session.mic_source = None
        source = self.start()
        self.assertEqual(source, self.session.sink_name + ".monitor")
        self.assertFalse(self.session.enabled)
        self.assertEqual([m[0] for m in self.pulse.modules.values()], ["module-null-sink"])
        self.assertNotIn(("get-default-source",), self.pulse.calls)
        self.assertEqual(len(self.pulse.outputs), 1)

    def test_on_off_owns_loopback_with_fixed_endpoints_and_never_moves_recorder(self):
        self.start()
        recorder = copy.deepcopy(self.pulse.outputs[0])
        self.session.set_enabled(True)
        self.assertTrue(self.session.enabled)
        load = next(c for c in self.pulse.calls if c[:2] == ("load-module", "module-loopback"))
        self.assertIn("source_dont_move=true", load)
        self.assertIn("sink_dont_move=true", load)
        self.assertIn("source=test.monitor", load)
        self.session.set_enabled(False)
        self.assertFalse(self.session.enabled)
        self.assertEqual(self.pulse.outputs, [recorder])
        self.assertEqual([m[0] for m in self.pulse.modules.values()], ["module-null-sink"])
        self.assertFalse(any(c[0].startswith(("set-", "move-")) for c in self.pulse.calls))

    def test_default_resolved_when_enabling_and_again_after_off(self):
        self.session.mic_source = None
        self.start()
        self.pulse.default = "other.monitor"
        self.session.set_enabled(True)
        self.assertEqual(self.session.selected_index, 11)
        self.session.set_enabled(False)
        self.pulse.default = "test.monitor"
        self.session.set_enabled(True)
        self.assertEqual(self.session.selected_index, 10)

    def test_initially_enabled_opens_microphone_only_after_binding(self):
        self.session.initial_enabled = True
        self.session.start()
        self.assertFalse(self.session.enabled)
        self.assertEqual(len(self.pulse.modules), 1)
        self.pulse.reader()
        self.session.bind(123)
        self.assertTrue(self.session.enabled)

    def test_bind_selects_private_reader_not_same_pid_desktop_audio(self):
        self.session.start()
        self.pulse.reader(source=10, index=70)
        self.pulse.reader(pid=999, index=71)
        self.pulse.reader()
        self.session.bind(123)
        self.assertEqual(self.session.bound_stream, 51)
        with self.assertRaisesRegex(AudioError, "another recording"):
            self.session.bind(999)

    def test_ambiguous_private_readers_rejected(self):
        self.session.start()
        self.pulse.reader()
        self.pulse.reader(index=72)
        with self.assertRaisesRegex(AudioError, "one private"):
            self.session.bind(123)
        self.assertFalse(self.session.enabled)

    def test_dead_or_reused_recording_pid_never_opens_microphone(self):
        self.start()
        self.identity.return_value = "replacement"
        with self.assertRaisesRegex(AudioError, "no longer"):
            self.session.set_enabled(True)
        self.assertEqual(len(self.pulse.modules), 1)

    def test_disconnect_disables_feed_without_default_fallback(self):
        self.start(enabled=True)
        self.pulse.sources = [s for s in self.pulse.sources if s["name"] != "test.monitor"]
        self.session.refresh()
        self.assertFalse(self.session.enabled)
        self.assertIn("disconnected", self.session.error)
        self.assertEqual(len(self.pulse.modules), 1)

    def test_external_route_change_turns_feed_off(self):
        self.start(enabled=True)
        self.pulse.inputs[0]["sink"] = 999
        self.session.refresh()
        self.assertFalse(self.session.enabled)
        self.assertIn("route changed", self.session.error)

    def test_end_of_recording_closes_physical_feed(self):
        self.start(enabled=True)
        self.pulse.outputs = [o for o in self.pulse.outputs if o["index"] != 51]
        self.session.refresh()
        self.assertFalse(self.session.enabled)
        self.assertIn("recording ended", self.session.error)

    def test_failed_off_does_not_claim_disabled(self):
        self.start(enabled=True)
        self.pulse.fail_unload = True
        with self.assertRaisesRegex(AudioError, "Permission denied"):
            self.session.set_enabled(False)
        self.assertTrue(self.session.enabled)
        self.assertEqual(len(self.pulse.modules), 2)

    def test_close_with_live_reader_disables_feed_but_preserves_silent_source(self):
        self.start(enabled=True)
        with self.assertRaisesRegex(AudioError, "release its private"):
            self.session.close()
        self.assertFalse(self.session.enabled)
        self.assertEqual(len(self.pulse.modules), 1)
        self.assertFalse(self.session.closed)
        self.pulse.outputs.clear()
        self.session.close()
        self.assertTrue(self.session.closed)
        self.assertFalse(self.pulse.modules)

    def test_close_refuses_unload_even_if_reader_belongs_to_another_app(self):
        self.start()
        self.pulse.outputs.clear()
        self.pulse.reader(pid=999)
        with self.assertRaisesRegex(AudioError, "release its private"):
            self.session.close()
        self.assertEqual(len(self.pulse.modules), 1)

    def test_reused_module_id_is_not_unloaded(self):
        self.start(enabled=True)
        index = self.session.loop_module
        self.pulse.modules[index] = ("module-loopback", ("sink=unrelated", "source_output_properties=application.name=AnotherApp"))
        self.session.set_enabled(False)
        self.assertIn(index, self.pulse.modules)
        self.assertNotIn(("unload-module", str(index)), self.pulse.calls)

    def test_source_argument_injection_and_unavailable_source_rejected(self):
        self.start()
        for source in ["bad source_dont_move=false", 'bad"source', "missing"]:
            self.session.mic_source = source
            with self.assertRaises(AudioError):
                self.session.set_enabled(True)
        self.assertEqual(len(self.pulse.modules), 1)

    def test_no_bound_reader_cannot_open_microphone(self):
        self.session.start()
        with self.assertRaisesRegex(AudioError, "Start recording"):
            self.session.set_enabled(True)
        self.assertEqual(len(self.pulse.modules), 1)

    def test_start_failure_finds_own_module_even_when_reply_is_invalid(self):
        original = self.pulse.__call__

        def invalid_reply(*args):
            reply = original(*args)
            return "lost acknowledgement" if args[0] == "load-module" else reply

        with patch("lumen.microphone_worker._run", side_effect=invalid_reply):
            with self.assertRaises(AudioError):
                self.session.start()
        self.assertFalse(self.pulse.modules)


class MicrophoneControllerTests(TestCase):
    def controller(self, *, exited=False):
        controller = LiveMicrophone("test.monitor")
        process = Mock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.poll.return_value = 0 if exited else None

        def finished(timeout):
            process.poll.return_value = 0
            return 0

        process.wait.side_effect = finished
        controller.process = process
        controller._reader = Mock()
        return controller

    def test_close_after_failed_start_waits_for_eof_cleanup_without_writing_closed_pipe(self):
        controller = self.controller()
        controller.process.stdin.close()
        controller._reader.join.side_effect = lambda **kw: setattr(controller, "_closed", True)
        with patch.object(controller, "_request") as request:
            controller.close()
        request.assert_not_called()
        controller.process.wait.assert_called_once_with(timeout=15)
        self.assertTrue(controller.process.stdout.closed)

    def test_close_drains_last_closed_state_before_judging_exited_guardian(self):
        controller = self.controller(exited=True)
        controller._reader.join.side_effect = lambda **kw: setattr(controller, "_closed", True)
        controller.close()
        self.assertTrue(controller._closed)
        self.assertTrue(controller.process.stdin.closed)
        self.assertTrue(controller.process.stdout.closed)

    def test_dead_guardian_without_cleanup_acknowledgement_reports_error_and_closes_pipes(self):
        controller = self.controller(exited=True)
        with self.assertRaisesRegex(MicrophoneError, "could not be confirmed"):
            controller.close()
        self.assertTrue(controller.process.stdin.closed)
        self.assertTrue(controller.process.stdout.closed)

    def test_closed_control_pipe_is_an_application_error_not_valueerror(self):
        controller = self.controller()
        controller.process.stdin.close()
        with self.assertRaisesRegex(MicrophoneError, "unavailable"):
            controller.set_enabled(True)

    def test_enabled_never_runs_audio_commands(self):
        controller = self.controller()
        controller._enabled = True
        with patch.object(controller, "_request") as request:
            self.assertTrue(controller.enabled)
        request.assert_not_called()

