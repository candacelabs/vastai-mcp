# Vast.ai MCP for Codex

Search the Vast.ai Python SDK offline, compare live GPU offers, and manage GPU rentals from Codex. This stdio server starts when Codex connects. It uses the Python SDK maintained in [vast-ai/vast-cli](https://github.com/vast-ai/vast-cli).

## Set up

Run these commands from this checkout with Python 3.11 or later and `uv`.

```bash
uv sync --locked
codex mcp add vastai -- "$(pwd)/.venv/bin/vastai-mcp"
codex mcp get vastai
```

To use an exported `VAST_API_KEY`, add this line to the existing `[mcp_servers.vastai]` section in `~/.codex/config.toml`. Start Codex from a shell that has the key.

```toml
env_vars = ["VAST_API_KEY", "XDG_CONFIG_HOME"]
```

The SDK also reads its CLI credential file at `$XDG_CONFIG_HOME/vastai/vast_api_key`, normally `~/.config/vastai/vast_api_key`. SDK lookup and public offer searches work without credentials. Public offer search also retries anonymously if Vast rejects the configured key. Ask Codex to call `auth_status` to check local key discovery or `list_instances` to check live account access.

## Use the tools

| Tools | What they do |
| --- | --- |
| `sdk_search`, `sdk_describe` | Find names by fuzzy match, then read the exact signature, docstring, and pinned source. |
| `search_offers` | Filter live offers and compare estimated hourly costs for an allocation. |
| `auth_status`, `list_instances`, `get_instance` | Check credentials and inspect rentals, status, SSH details, and ports. |
| `create_instance`, `start_instance`, `stop_instance`, `destroy_instance` | Rent an offer and manage its lifecycle. Stopped instances can still accrue storage charges. Destroying an instance removes its local data. |
| `instance_logs`, `list_endpoints` | Inspect workload logs and serverless endpoint groups. |

For example, ask Codex to call `search_offers` with these arguments to price an H100 SXM rental with 85 GB of disk.

```json
{
  "query": "num_gpus=1 gpu_name=H100_SXM disk_space>=85",
  "storage_gb": 85,
  "order": "dph_total",
  "limit": 5
}
```

`disk_space` filters the available capacity. `storage_gb` tells Vast how much storage to price. To rent a selected offer, `create_instance` takes its current `offer_id`, `disk_gb`, and an image or template hash. It accepts Vast Docker options in `env`, multiline startup commands in `onstart_cmd`, and `ssh` and `direct` flags. A successful rental starts billing.

## SDK index and tests

The bundled index contains 557 public Python declarations, including 135 methods on `VastAI`. It covers the SDK and its supporting modules, excluding private modules, CLI commands, and PDF generation. The `vastai://sdk` resource reports the version, commit, and symbol count. The twelve callable tools above expose the supported operations. The SDK docstrings are distributed under the [upstream MIT license](VAST-LICENSE.txt).

To rebuild the index from the maintained repository and run the protocol tests, use the following commands from this checkout. The tests send SDK requests to a local HTTP fixture.

```bash
git clone https://github.com/vast-ai/vast-cli ../vast-cli  # if absent
uv run --locked python scripts/index_sdk.py ../vast-cli
uv run --locked python -m unittest discover -s tests -v
```

The indexer reads the Git tag pinned by `pyproject.toml`, currently [v1.12.0](https://github.com/vast-ai/vast-cli/tree/50c16ea5bdad0605baf57167f18b7b6a93ed661d).
