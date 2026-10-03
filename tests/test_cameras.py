"""Read-only V4L2 discovery selects capture nodes and stable identities."""

import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from lumen import system


CAPTURE = 0x00000001
MPLANE = 0x00001000
STREAMING = 0x04000000
DEVICE_CAPS = 0x80000000


class CameraDiscoveryTests(TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="lumen-camera-discovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.dev = self.root / "dev"
        self.sys = self.root / "sys"
        self.dev.mkdir()
        self.sys.mkdir()
        self.info = {}

    def node(self, number, *, caps=CAPTURE | STREAMING, bus="usb-camera", index=0, name="USB camera"):
        path = self.dev / f"video{number}"
        path.touch()
        folder = self.sys / path.name
        folder.mkdir()
        (folder / "index").write_text(str(index))
        self.info[path] = {
            "name": name, "driver": "uvcvideo", "bus_info": bus,
            "capabilities": caps, "device_number": number + 100,
        }
        return path

    def alias(self, folder, name, target):
        directory = self.dev / "v4l" / folder
        directory.mkdir(parents=True, exist_ok=True)
        alias = directory / name
        alias.symlink_to(target)
        return alias

    def discover(self):
        with patch.object(system, "_query_camera", side_effect=lambda p: self.info[p]):
            return system.get_cameras(sys_root=self.sys, dev_root=self.dev)

    def test_filters_metadata_output_codecs_and_nodes_without_io_support(self):
        first = self.node(0)
        self.node(1, caps=0x00800000 | STREAMING, index=1)  # Metadata duplicate.
        self.node(2, caps=0x00000002 | STREAMING)  # Inactive exclusive loopback.
        self.node(3, caps=CAPTURE | STREAMING | 0x00008000)  # Codec/M2M device.
        self.node(4, caps=CAPTURE)  # Cannot supply frames through either API.
        multi = self.node(10, caps=MPLANE | STREAMING, bus="usb-second")
        cameras = self.discover()
        self.assertEqual([c["path"] for c in cameras], [str(first), str(multi)])

    def test_serial_alias_precedes_connection_alias_and_identity_survives_renumbering(self):
        original = self.node(0)
        by_id = self.alias("by-id", "usb-SerialCamera-video-index0", original)
        self.alias("by-path", "pci-port-video-index0", original)
        initial = self.discover()[0]
        replacement = self.node(8)
        original.unlink()
        by_id.unlink()
        by_id.symlink_to(replacement)
        refreshed = self.discover()[0]
        self.assertEqual(initial["id"], refreshed["id"])
        self.assertEqual(refreshed["id"], str(by_id))
        self.assertEqual(refreshed["path"], str(replacement))
        self.assertTrue(refreshed["identity_stable"])

    def test_connection_alias_then_bus_index_are_used_without_serial_alias(self):
        node = self.node(0)
        by_path = self.alias("by-path", "pci-camera-video-index0", node)
        self.assertEqual(self.discover()[0]["id"], str(by_path))
        by_path.unlink()
        camera = self.discover()[0]
        self.assertEqual(camera["id"], "v4l2:uvcvideo:usb-camera:index:0")
        self.assertIsNone(camera["stable_path"])
        self.assertTrue(camera["identity_stable"])

    def test_aliases_deduplicate_without_hiding_distinct_capture_endpoints(self):
        first = self.node(0)
        duplicate = self.node(2)
        self.info[duplicate]["device_number"] = self.info[first]["device_number"]
        (self.dev / "video3").symlink_to(first)
        other = self.node(4, index=2, name="USB camera infrared")
        cameras = self.discover()
        self.assertEqual([c["path"] for c in cameras], [str(first), str(other)])
        self.assertNotEqual(cameras[0]["id"], cameras[1]["id"])

    def test_inaccessible_or_disconnected_nodes_do_not_break_discovery(self):
        bad, good = self.node(0), self.node(5, bus="other")
        (self.dev / "video7").symlink_to(self.dev / "missing")
        self.alias("by-id", "broken-link", self.dev / "missing")

        def query(path):
            if path == bad:
                raise PermissionError("Cannot query this device")
            return self.info[path]

        with patch.object(system, "_query_camera", side_effect=query):
            cameras = system.get_cameras(sys_root=self.sys, dev_root=self.dev)
        self.assertEqual([c["path"] for c in cameras], [str(good)])

    def test_identity_without_alias_bus_or_index_is_explicitly_unstable(self):
        path = self.node(0, bus="", name="")
        (self.sys / "video0" / "index").unlink()
        camera = self.discover()[0]
        self.assertEqual(camera["id"], str(path))
        self.assertFalse(camera["identity_stable"])
        self.assertEqual(camera["label"], "video0 · video0")


class QueryCameraTests(TestCase):
    def query(self, capabilities, device_caps):
        data = system._VIDEO_CAPABILITY.pack(
            b"uvcvideo", b"A camera\0trailing", b"usb-test", 1,
            capabilities, device_caps, 0, 0, 0,
        )

        def ioctl(fd, request, target, mutate):
            self.assertEqual(request, 0x80685600)
            self.assertEqual(len(target), 104)
            target[:] = data

        with (
            patch.object(system.os, "open", return_value=42) as opened,
            patch.object(system.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=81)),
            patch.object(system.os, "close") as closed,
            patch.object(system.fcntl, "ioctl", side_effect=ioctl) as queried,
        ):
            result = system._query_camera(Path("/dev/video0"))
        self.assertEqual(opened.call_args.args[1] & os.O_ACCMODE, os.O_RDONLY)
        self.assertTrue(opened.call_args.args[1] & os.O_NONBLOCK)
        closed.assert_called_once_with(42)
        queried.assert_called_once()
        self.assertEqual(result["name"], "A camera")
        return result

    def test_device_caps_override_aggregate_device_capture_claim(self):
        metadata_only = 0x00800000 | STREAMING
        result = self.query(DEVICE_CAPS | CAPTURE | metadata_only, metadata_only)
        self.assertEqual(result["capabilities"], metadata_only)

    def test_legacy_capabilities_work_when_device_caps_flag_is_absent(self):
        self.assertEqual(self.query(CAPTURE | STREAMING, 0)["capabilities"], CAPTURE | STREAMING)

    def test_failed_ioctl_closes_descriptor(self):
        with (
            patch.object(system.os, "open", return_value=42),
            patch.object(system.os, "fstat", return_value=SimpleNamespace(st_mode=stat.S_IFCHR, st_rdev=81)),
            patch.object(system.os, "close") as closed,
            patch.object(system.fcntl, "ioctl", side_effect=OSError("Disconnected")),
        ):
            with self.assertRaises(OSError):
                system._query_camera(Path("/dev/video0"))
        closed.assert_called_once_with(42)
