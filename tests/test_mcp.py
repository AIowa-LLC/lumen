"""Official MCP protocol, schema validation, structured results, and errors."""

import importlib.util
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

SDK = importlib.util.find_spec("mcp") is not None
if SDK:
    from mcp import Client
    from lumen.mcp_server import NativeBridge, create_server


class Bridge:
    def __init__(self):
        self.calls = []

    async def call(self, operation, **arguments):
        self.calls.append((operation, arguments))
        if arguments.get("project_id") == "missing":
            raise ValueError("Take not found")
        return {"operation": operation, "arguments": arguments}


@unittest.skipUnless(SDK, "Optional MCP SDK required; install the mcp extra")
class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_modern_and_legacy_protocol_tool_discovery_and_structured_results(self):
        for mode in ("auto", "legacy"):
            async with Client(create_server(Bridge()), mode=mode, read_timeout_seconds=5) as client:
                tools = (await client.list_tools()).tools
                self.assertEqual(len(tools), 20)
                status = next(tool for tool in tools if tool.name == "lumen_status")
                self.assertTrue(status.annotations.read_only_hint)
                result = await client.call_tool("lumen_status")
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content["operation"], "status")

    async def test_patch_is_sparse_and_types_are_strict(self):
        bridge = Bridge()
        async with Client(create_server(bridge), read_timeout_seconds=5) as client:
            result = await client.call_tool("lumen_update_edits", {"project_id": "demo", "changes": {"padding": 80}})
            self.assertEqual(result.structured_content["arguments"]["changes"], {"padding": 80})
            for changes in ({"padding": "80"}, {"padding": 301}, {"mystery": True}, {"speed": 4}):
                result = await client.call_tool("lumen_update_edits", {"project_id": "demo", "changes": changes})
                self.assertTrue(result.is_error)
            self.assertEqual(len(bridge.calls), 1)

    async def test_capture_defaults_do_not_enable_audio_and_unknown_settings_fail(self):
        bridge = Bridge()
        async with Client(create_server(bridge), read_timeout_seconds=5) as client:
            result = await client.call_tool("lumen_start_recording", {"options": {"mode": "monitor"}})
            self.assertEqual(result.structured_content["arguments"]["options"]["audio"], "none")
            result = await client.call_tool("lumen_start_recording", {"options": {"fps": 999}})
            self.assertTrue(result.is_error)
            result = await client.call_tool("lumen_start_recording", {"options": {"shell": "command"}})
            self.assertTrue(result.is_error)
            self.assertEqual(len(bridge.calls), 1)

    async def test_native_errors_reach_the_client_as_actionable_tool_errors(self):
        async with Client(create_server(Bridge()), read_timeout_seconds=5) as client:
            result = await client.call_tool("lumen_get_project", {"project_id": "missing"})
            self.assertTrue(result.is_error)
            self.assertIn("Take not found", result.content[0].text)

    async def test_resources_and_prompt_use_native_state_and_document_job_completion(self):
        async with Client(create_server(Bridge()), read_timeout_seconds=5) as client:
            resources = (await client.list_resources()).resources
            self.assertIn("lumen://status", [str(r.uri) for r in resources])
            resource = await client.read_resource("lumen://projects/demo")
            self.assertEqual(json.loads(resource.contents[0].text)["arguments"], {"project_id": "demo"})
            prompt = await client.get_prompt("make_demo", {"goal": "Show a product"})
            text = prompt.messages[0].content.text
            self.assertIn("Show a product", text)
            self.assertIn("poll", text)
            self.assertIn("Follow the user's capture boundaries", text)

    async def test_native_rpc_starts_host_then_uses_one_explicit_json_command(self):
        bridge = NativeBridge()
        bridge.ensure_app = AsyncMock()
        process = Mock(returncode=0, communicate=AsyncMock(return_value=(b'{"result":{"state":"idle"}}', b"")))
        with patch.dict(os.environ, {"LUMEN_LIBRARY": "/tmp/lumen-mcp-rpc-test"}), patch("lumen.mcp_server.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await bridge.call("status")
        self.assertEqual(result, {"state": "idle"})
        bridge.ensure_app.assert_awaited_once()
        args = spawn.call_args.args
        self.assertEqual(args[:5], ("/usr/bin/python", "-P", "-m", "lumen", "--agent"))
        self.assertEqual(json.loads(args[5]), {"operation": "status", "arguments": {}, "library": "/tmp/lumen-mcp-rpc-test"})

    async def test_native_rpc_timeout_reaps_its_command_and_reports_desktop_error(self):
        bridge = NativeBridge()
        bridge.ensure_app = AsyncMock()
        process = Mock(returncode=None, communicate=AsyncMock(side_effect=TimeoutError), wait=AsyncMock())
        with patch("lumen.mcp_server.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ValueError, "native Lumen app"):
                await bridge.call("status")
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()
