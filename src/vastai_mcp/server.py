"""Vast.ai SDK discovery and GPU rental tools over MCP."""

import argparse
from asyncio import to_thread
from contextlib import asynccontextmanager, nullcontext, redirect_stdout
from contextvars import ContextVar
import sys
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field
from requests import HTTPError, RequestException
from vastai import VastAI
from vastai.api import offers
from vastai.api.client import VastClient
from vastai.api.query import offers_alias, offers_fields, offers_mult, parse_order, parse_query

from . import catalog


_stdio = ContextVar("vastai_stdio", default=False)


@asynccontextmanager
async def sdk_diagnostics(_server):
    # The SDK can print diagnostics. Reserve stdout for protocol messages in stdio mode.
    with redirect_stdout(sys.stderr) if _stdio.get() else nullcontext():
        yield


mcp = FastMCP("vastai", lifespan=sdk_diagnostics, stateless_http=True, json_response=True,
              log_level="WARNING", instructions=(
    "Search the SDK locally with sdk_search and sdk_describe. search_offers reads current GPU prices. "
    "create_instance rents the specified offer. stop_instance retains storage and destroy_instance releases it."
))
READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
OFFLINE = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
CREATE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)
CHANGE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
PositiveID = Annotated[int, Field(gt=0)]
PageSize = Annotated[int, Field(ge=1, le=100)]
PositiveAmount = Annotated[float, Field(gt=0, allow_inf_nan=False)]

OFFER_FIELDS = ("id", "machine_id", "gpu_name", "num_gpus", "gpu_ram", "gpu_total_ram", "cuda_max_good",
                "driver_version", "cpu_ram", "disk_space", "dph_total", "min_bid", "storage_cost",
                "inet_down", "inet_up", "direct_port_count", "reliability", "geolocation", "duration")
INSTANCE_FIELDS = ("id", "machine_id", "label", "actual_status", "intended_status", "cur_state",
                   "status_msg", "gpu_name", "num_gpus", "gpu_ram", "dph_total", "disk_space",
                   "image_uuid", "ssh_host", "ssh_port", "public_ipaddr", "ports", "start_date")


def account(*, write=False):
    # Vast's retry parameter is the number of attempts. Never replay a rental after a lost reply.
    return VastAI(raw=True, quiet=True, retry=1 if write else 3)


def public_client():
    try:
        return account().client
    except RuntimeError as exc:
        if "No API key found" not in str(exc):
            raise
        return VastClient()


class VastAuthenticationError(ValueError):
    pass


async def request(fn, *args, **kwargs):
    try:
        result = await to_thread(fn, *args, **kwargs)
    except HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        if exc.response is not None:
            try:
                detail = exc.response.json()
            except ValueError:
                detail = None
            if isinstance(detail, dict) and detail.get("error") == "auth_error":
                raise VastAuthenticationError(f"Vast.ai rejected the credential (HTTP {status}, auth_error).") from None
        raise ValueError(f"Vast.ai returned HTTP {status} for {fn.__name__}.") from None
    except RequestException as exc:
        raise ValueError(f"{fn.__name__}: Vast.ai transport error ({type(exc).__name__}); "
                         "check instance state before repeating a write.") from None
    if isinstance(result, dict) and (result.get("success") is False or result.get("error")):
        raise ValueError(f"Vast.ai declined {fn.__name__}: {result.get('msg') or result.get('error') or 'unspecified reason'}")
    return result


def select(row, fields):
    return {key: row[key] for key in fields if key in row}


@mcp.resource("vastai://sdk")
def sdk_info() -> dict[str, Any]:
    """Revision, upstream source, and size of the local searchable SDK index."""
    return {"version": catalog.INDEX["version"], "revision": catalog.INDEX["revision"],
            "symbols": len(catalog.SYMBOLS), "repository": "https://github.com/vast-ai/vast-cli"}


@mcp.tool(annotations=OFFLINE)
def sdk_search(query: str, limit: PageSize = 10) -> list[dict[str, Any]]:
    """Find SDK classes, methods and functions by name, keywords or approximate spelling. Works offline."""
    return catalog.search(query, limit)


@mcp.tool(annotations=OFFLINE)
def sdk_describe(symbol: str) -> dict[str, Any]:
    """Read an indexed symbol's exact signature, docstring, source path, line, and pinned GitHub link."""
    return catalog.describe(symbol)


@mcp.tool(annotations=OFFLINE)
def auth_status() -> dict[str, Any]:
    """Report whether the SDK can find a credential locally. This does not test the credential against Vast."""
    try:
        account()
    except RuntimeError as exc:
        if "No API key found" not in str(exc):
            raise
        return {"configured": False, "setup": "Export VAST_API_KEY for Codex or configure the Vast CLI key file."}
    return {"configured": True}


@mcp.tool(annotations=READ)
async def search_offers(query: str | dict = "", limit: PageSize = 10, order: str = "dph_total",
                  offer_type: Literal["on-demand", "bid", "reserved"] = "on-demand",
                  storage_gb: PositiveAmount = 10) -> list[dict[str, Any]]:
    """Search live, rentable GPU offers. Example: num_gpus=1 gpu_ram>=80 disk_space>=85.
    String filters use SDK units (gpu_ram in GB). Dict filters use API units. storage_gb sets pricing.
    Results include offer id, GPU memory in MB, and estimated USD/hour in dph_total.
    """
    if isinstance(query, str):
        query = parse_query(query, {}, offers_fields, offers_alias, offers_mult)
    client = public_client()
    params = dict(query=query, offer_type=offer_type, order=parse_order(order), limit=limit, storage=storage_gb)
    try:
        rows = await request(offers.search_offers, client, **params)
    except VastAuthenticationError:
        if not client.api_key:
            raise
        rows = await request(offers.search_offers, VastClient(), **params)
    return [select(row, OFFER_FIELDS) for row in rows]


@mcp.tool(annotations=READ)
async def list_instances(limit: PageSize = 25, after_token: str | None = None) -> dict[str, Any]:
    """List a page of account instances and return next_token for the following page."""
    params = {"limit": limit, "order_by": [{"col": "id", "dir": "asc"}]}
    if after_token:
        params["after_token"] = after_token
    data = await request(account().show_instances_v1, params)
    return {"instances": [select(row, INSTANCE_FIELDS) for row in data["instances"]],
            "next_token": data.get("next_token"), "total_instances": data.get("total_instances")}


@mcp.tool(annotations=READ)
async def get_instance(instance_id: PositiveID) -> dict[str, Any]:
    """Get instance state, GPU, price, SSH address and exposed ports by contract ID."""
    row = await request(account().show_instance, instance_id)
    if row is None:
        raise ValueError(f"Vast.ai found no instance with ID {instance_id}")
    return select(row, INSTANCE_FIELDS)


@mcp.tool(annotations=CREATE)
async def create_instance(offer_id: PositiveID, disk_gb: PositiveAmount, image: str | None = None,
                    template_hash: str | None = None, env: str | dict | None = None,
                    onstart_cmd: str | None = None, label: str | None = None, ssh: bool = False,
                    direct: bool = False, jupyter: bool = False, bid_price: PositiveAmount | None = None) -> dict[str, Any]:
    """Rent a live offer; billing starts on success. Supply an image or a Vast template hash.
    env accepts Vast Docker options such as '-p 3000:3000 -e MODEL_NAME="owner/model"'.
    onstart_cmd is sent to the rented container. Set bid_price (USD/hour) for an interruptible rental.
    """
    if not (image or template_hash):
        raise ValueError("Supply image or template_hash.")
    return await request(account(write=True).create_instance, offer_id, image=image, template_hash=template_hash,
                   disk=disk_gb, env=env, onstart_cmd=onstart_cmd, label=label, ssh=ssh, direct=direct,
                   jupyter=jupyter, bid_price=bid_price)


@mcp.tool(annotations=CHANGE)
async def start_instance(instance_id: PositiveID) -> dict[str, Any]:
    """Start a stopped instance and resume GPU billing."""
    return await request(account(write=True).start_instance, instance_id)


@mcp.tool(annotations=CHANGE)
async def stop_instance(instance_id: PositiveID) -> dict[str, Any]:
    """Stop an instance. Persistent storage remains billable."""
    return await request(account(write=True).stop_instance, instance_id)


@mcp.tool(annotations=CHANGE)
async def destroy_instance(instance_id: PositiveID) -> dict[str, Any]:
    """Destroy the specified rental and its instance-local data."""
    return await request(account(write=True).destroy_instance, instance_id)


@mcp.tool(annotations=READ)
async def instance_logs(instance_id: PositiveID, tail: Annotated[int, Field(ge=1, le=1000)] = 100,
                  filter: str | None = None, daemon_logs: bool = False) -> dict[str, Any]:
    """Get the last lines of workload or daemon logs from the specified rental."""
    return {"logs": await request(account().logs, instance_id, tail=str(tail), filter=filter, daemon_logs=daemon_logs)}


@mcp.tool(annotations=READ)
async def list_endpoints() -> list[dict[str, Any]]:
    """List account serverless endpoint groups and their capacity settings."""
    rows = await request(account().show_endpoints)
    fields = ("id", "endptjob_id", "endpoint_name", "endpoint_state", "url", "min_load",
              "target_util", "cold_workers", "max_workers", "created_at")
    return [select(row, fields) for row in rows]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", action="store_true", help="Serve /mcp with FastAPI on 127.0.0.1")
    parser.add_argument("--port", type=int, help="Local HTTP port (default 8765)")
    args = parser.parse_args()
    if args.http:
        import os
        import uvicorn
        from .http import create_app

        token = os.environ.get("VASTAI_MCP_TOKEN")
        if not token:
            parser.error("--http requires VASTAI_MCP_TOKEN for incoming requests")
        if args.port is not None and not 0 <= args.port <= 65535:
            parser.error("--port must be between 0 and 65535")
        uvicorn.run(create_app(mcp, token), host="127.0.0.1", port=8765 if args.port is None else args.port)
    else:
        if args.port is not None:
            parser.error("--port requires --http")
        token = _stdio.set(True)
        try:
            mcp.run(transport="stdio")
        finally:
            _stdio.reset(token)
