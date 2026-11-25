import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit

import pytest
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
            self.server.listing_started.set()
            self.server.allow_listing.wait(timeout=8)
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


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api():
    with ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server:
        server.calls = []
        server.reject_key = False
        server.listing_started = threading.Event()
        server.allow_listing = threading.Event()
        server.allow_listing.set()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.allow_listing.set()
            server.shutdown()
            thread.join()


@pytest.fixture
async def session(api):
    params = StdioServerParameters(
        command=str(Path(sys.executable).with_name("vastai-mcp")),
        env={"VAST_URL": f"http://127.0.0.1:{api.server_port}", "VAST_API_KEY": "test-fixture"},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
        await client.initialize()
        yield client


async def call(session, tool, **arguments):
    result = await session.call_tool(tool, arguments)
    assert not result.isError, result.content
    return result.structuredContent


async def test_discovery_and_offer_search(session, api):
    tools = await session.list_tools()
    assert len(tools.tools) == 12
    meta = await session.read_resource("vastai://sdk")
    assert json.loads(meta.contents[0].text)["symbols"] == 557
    hits = await call(session, "sdk_search", query="creat instnce", limit=1)
    assert hits["result"][0]["name"] == "vastai.sdk.VastAI.create_instance"
    item = await call(session, "sdk_describe", symbol="vastai.VastAI.create_instance")
    assert "onstart_cmd" in item["signature"]
    assert item["revision"] in item["source"]
    assert (await call(session, "auth_status"))["configured"]
    assert api.calls == []
    offers = await call(session, "search_offers", query="gpu_ram>=80 disk_space>=85", storage_gb=85)
    assert offers["result"][0]["id"] == 31
    method, path, _, body = api.calls[-1]
    assert (method, path) == ("POST", "/api/v0/bundles/")
    assert body["gpu_ram"] == {"gte": 80000}
    assert body["allocated_storage"] == 85


async def test_sdk_lookup_during_pending_http_request(session, api):
    api.allow_listing.clear()
    listing = asyncio.create_task(call(session, "list_instances"))
    try:
        assert await asyncio.to_thread(api.listing_started.wait, 4)
        lookup = await asyncio.wait_for(call(session, "sdk_describe", symbol="VastAI.create_instance"), 3)
        assert "onstart_cmd" in lookup["signature"]
        assert not listing.done()
    finally:
        api.allow_listing.set()
        await listing


async def test_lifecycle_on_loopback(session, api):
    startup = 'export HF_TOKEN="${HF_TOKEN:-1}"\nentrypoint.sh &\nwait'
    created = await call(session, "create_instance", offer_id=31, disk_gb=85, image="vastai/vllm:fixture",
                         env='-p 3000:3000 -e MODEL_NAME="org/model"', onstart_cmd=startup,
                         label="gpu-test", ssh=True, direct=True)
    assert created["new_contract"] == 47
    method, path, _, body = api.calls[-1]
    assert (method, path) == ("PUT", "/api/v0/asks/31/")
    assert (body["disk"], body["image"], body["label"]) == (85, "vastai/vllm:fixture", "gpu-test")
    assert body["env"] == {"-p 3000:3000": "1", "MODEL_NAME": "org/model"}
    assert body["onstart"] == startup
    assert body["runtype"] == "ssh_direc ssh_proxy"
    page = await call(session, "list_instances", limit=2, after_token="page-2")
    assert page["next_token"] == "page-3"
    assert api.calls[-1][2]["after_token"] == ["page-2"]
    assert "extra_env" not in page["instances"][0]
    instance = await call(session, "get_instance", instance_id=47)
    assert instance["actual_status"] == "running"
    assert "extra_env" not in instance
    logs = await call(session, "instance_logs", instance_id=47, tail=3)
    assert logs["logs"] == "worker ready\n"
    endpoints = await call(session, "list_endpoints")
    assert endpoints["result"] == [{"id": 56, "endpoint_name": "gpu"}]
    for tool, state in [("stop_instance", "stopped"), ("start_instance", "running")]:
        await call(session, tool, instance_id=47)
        assert api.calls[-1] == ("PUT", "/api/v0/instances/47/", {}, {"state": state})
    await call(session, "destroy_instance", instance_id=47)
    assert api.calls[-1][:2] == ("DELETE", "/api/v0/instances/47/")


async def test_rejected_credentials_allow_public_offers(session, api):
    api.reject_key = True
    offers = await call(session, "search_offers", limit=1)
    assert offers["result"][0]["id"] == 31
    assert [c[1] for c in api.calls] == ["/api/v0/bundles/"] * 2
    listing = await session.call_tool("list_instances", {"limit": 1})
    assert listing.isError
    assert "auth_error" in listing.content[0].text
    assert len(api.calls) == 3


async def test_rental_failure_is_reported_without_retry(session, api):
    result = await session.call_tool("create_instance", {"offer_id": 999, "disk_gb": 85, "image": "example"})
    assert result.isError
    assert "HTTP 503" in result.content[0].text
    assert len(api.calls) == 1
