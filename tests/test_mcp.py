from functools import wraps
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class Fixture(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def handle_request(self):
        url = urlsplit(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        self.server.calls.append((self.command, url.path, parse_qs(url.query), body))
        row = {"id": 47, "actual_status": "running", "start_date": 100, "extra_env": [["ACCESS", "private-value"]]}
        status = 200
        auth = self.headers.get("Authorization")
        if self.server.reject_key and auth:
            status, data = 404, {"error": "auth_error"}
        elif auth != "Bearer test-fixture" and url.path != "/api/v0/bundles/":
            status, data = 403, {"error": "auth_error"}
        elif url.path == "/api/v0/asks/999/":
            status, data = 503, {"error": "unavailable"}
        elif url.path == "/api/v0/asks/31/":
            data = {"success": True, "new_contract": 47}
        elif url.path == "/api/v1/instances/":
            data = {"instances": [row], "next_token": "page-3", "total_instances": 7}
        elif url.path == "/api/v0/instances/47/":
            data = {"instances": row} if self.command == "GET" else {"success": True}
        elif url.path == "/api/v0/bundles/":
            data = {"offers": [{"id": 31, "gpu_name": "H100 SXM", "dph_total": 1.23}]}
        elif url.path == "/api/v0/endptjobs/":
            data = {"success": True, "results": [{"id": 56, "endpoint_name": "gpu", "api_key": "private-value"}]}
        elif url.path == "/api/v0/instances/request_logs/47/":
            data = {"result_url": f"http://127.0.0.1:{self.server.server_port}/log-47"}
        else:
            status, data = 404, {"error": "missing_route"}
        # The SDK fetches the generated log URL as a separate unauthenticated GET.
        if url.path == "/log-47" and self.command == "GET":
            status, payload = 200, b"worker ready\n"
        else:
            payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_DELETE = handle_request


def with_mcp(test):
    @wraps(test)
    async def run(self):
        params = StdioServerParameters(
            command=str(Path(sys.executable).with_name("vastai-mcp")),
            env={"VAST_URL": f"http://127.0.0.1:{self.api.server_port}", "VAST_API_KEY": "test-fixture"},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            self.session = session
            await session.initialize()
            await test(self)
    return run


class VastMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        self.api.calls = []
        self.api.reject_key = False
        thread = threading.Thread(target=self.api.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(self.api.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.api.shutdown)

    async def call(self, name, **arguments):
        result = await self.session.call_tool(name, arguments)
        self.assertFalse(result.isError, result.content)
        return result.structuredContent

    @with_mcp
    async def test_discovery_and_offer_search(self):
        tools = await self.session.list_tools()
        self.assertEqual(len(tools.tools), 12)
        meta = await self.session.read_resource("vastai://sdk")
        self.assertEqual(json.loads(meta.contents[0].text)["symbols"], 557)
        hits = await self.call("sdk_search", query="creat instnce", limit=1)
        self.assertEqual(hits["result"][0]["name"], "vastai.sdk.VastAI.create_instance")
        item = await self.call("sdk_describe", symbol="vastai.VastAI.create_instance")
        self.assertIn("onstart_cmd", item["signature"])
        self.assertIn(item["revision"], item["source"])
        self.assertTrue((await self.call("auth_status"))["configured"])
        self.assertEqual(self.api.calls, [])
        offers = await self.call("search_offers", query="gpu_ram>=80 disk_space>=85", storage_gb=85)
        self.assertEqual(offers["result"][0]["id"], 31)
        method, path, _, body = self.api.calls[-1]
        self.assertEqual((method, path), ("POST", "/api/v0/bundles/"))
        self.assertEqual(body["gpu_ram"], {"gte": 80000})
        self.assertEqual(body["allocated_storage"], 85)

    @with_mcp
    async def test_lifecycle_on_loopback(self):
        startup = 'export HF_TOKEN="${HF_TOKEN:-1}"\nentrypoint.sh &\nwait'
        created = await self.call("create_instance", offer_id=31, disk_gb=85, image="vastai/vllm:fixture",
                                  env='-p 3000:3000 -e MODEL_NAME="org/model"', onstart_cmd=startup,
                                  label="gpu-test", ssh=True, direct=True)
        self.assertEqual(created["new_contract"], 47)
        method, path, _, body = self.api.calls[-1]
        self.assertEqual((method, path), ("PUT", "/api/v0/asks/31/"))
        self.assertEqual((body["disk"], body["image"], body["label"]), (85, "vastai/vllm:fixture", "gpu-test"))
        self.assertEqual(body["env"], {"-p 3000:3000": "1", "MODEL_NAME": "org/model"})
        self.assertEqual(body["onstart"], startup)
        self.assertEqual(body["runtype"], "ssh_direc ssh_proxy")
        page = await self.call("list_instances", limit=2, after_token="page-2")
        self.assertEqual(page["next_token"], "page-3")
        self.assertEqual(self.api.calls[-1][2]["after_token"], ["page-2"])
        self.assertNotIn("extra_env", page["instances"][0])
        instance = await self.call("get_instance", instance_id=47)
        self.assertEqual(instance["actual_status"], "running")
        self.assertNotIn("extra_env", instance)
        logs = await self.call("instance_logs", instance_id=47, tail=3)
        self.assertEqual(logs["logs"], "worker ready\n")
        endpoints = await self.call("list_endpoints")
        self.assertEqual(endpoints["result"], [{"id": 56, "endpoint_name": "gpu"}])
        for tool, state in [("stop_instance", "stopped"), ("start_instance", "running")]:
            await self.call(tool, instance_id=47)
            self.assertEqual(self.api.calls[-1], ("PUT", "/api/v0/instances/47/", {}, {"state": state}))
        await self.call("destroy_instance", instance_id=47)
        self.assertEqual(self.api.calls[-1][:2], ("DELETE", "/api/v0/instances/47/"))

    @with_mcp
    async def test_rejected_credentials_allow_public_offers(self):
        self.api.reject_key = True
        offers = await self.call("search_offers", limit=1)
        self.assertEqual(offers["result"][0]["id"], 31)
        self.assertEqual([c[1] for c in self.api.calls], ["/api/v0/bundles/"] * 2)
        listing = await self.session.call_tool("list_instances", {"limit": 1})
        self.assertTrue(listing.isError)
        self.assertIn("auth_error", listing.content[0].text)
        self.assertEqual(len(self.api.calls), 3)

    @with_mcp
    async def test_rental_failure_is_reported_without_retry(self):
        result = await self.session.call_tool("create_instance", {"offer_id": 999, "disk_gb": 85, "image": "example"})
        self.assertTrue(result.isError)
        self.assertIn("HTTP 503", result.content[0].text)
        self.assertEqual(len(self.api.calls), 1)
