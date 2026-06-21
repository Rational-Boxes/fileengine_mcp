# Examples

## `agent_demo.py` — read → write → time-travel recovery

Drives the server through its real MCP tool dispatch to demonstrate the
recoverability guarantee: an agent clobbers a file, then recovers the prior
version with the tools it already has.

```bash
PYTHONPATH=src:../python_interface python examples/agent_demo.py
```

## Connecting an MCP host

### Claude Desktop (stdio)

See [`claude_desktop_config.json`](./claude_desktop_config.json). Install the
server (`pip install -e .` + `pip install ../python_interface`), drop the
`mcpServers.fileengine` block into your `claude_desktop_config.json`, set the
LDAP credentials, and restart the app. The FileEngine tools appear in the host.

### Claude Code (stdio)

```bash
# from a checkout where `fileengine-mcp` is installed (or use an absolute path)
claude mcp add fileengine \
  --env FILEENGINE_MCP_USER=agent --env FILEENGINE_MCP_PASSWORD=… \
  --env FILEENGINE_LDAP_ENDPOINT=ldap://localhost:1389 \
  -- fileengine-mcp
claude mcp list          # shows fileengine: connected
```

### Claude Code / any host (remote, Streamable HTTP)

Start the HTTP transport (`fileengine-mcp-http`), then exchange credentials for a
bearer token and point the host at `/mcp`:

```bash
curl -sX POST http://HOST:8089/auth/token \
  -H 'content-type: application/json' \
  -d '{"username":"agent","password":"…"}'
# -> {"access_token":"TOKEN","token_type":"bearer","expires_in":3600}

claude mcp add --transport http fileengine http://HOST:8089/mcp \
  --header "Authorization: Bearer TOKEN" --header "X-Tenant: default"
```

Run the HTTP transport behind TLS (a reverse proxy) for anything non-local.
