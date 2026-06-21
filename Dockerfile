# FileEngine MCP server image.
#
# The server reuses the FileEngine Python client (`fileengine`, from the sibling
# python_interface/ checkout), so build with the *parent* directory as context:
#
#   docker build -f mcp/Dockerfile -t fileengine-mcp ..
#
# Run the Streamable HTTP transport (behind TLS in production); pass LDAP + core
# config via env (see .env-default):
#
#   docker run --rm -p 8089:8089 --env-file mcp/.env fileengine-mcp
#
# For stdio (an MCP host launches it), override the command:
#   docker run --rm -i --env-file mcp/.env fileengine-mcp fileengine-mcp
FROM python:3.12-slim

WORKDIR /app

# The reused client first (changes rarely → better layer caching), then the MCP.
# Copy only what's needed — never the gitignored .env (it holds credentials).
COPY python_interface/ /app/python_interface/
COPY mcp/pyproject.toml mcp/README.md /app/mcp/
COPY mcp/src/ /app/mcp/src/

RUN pip install --no-cache-dir /app/python_interface && \
    pip install --no-cache-dir /app/mcp

# Streamable HTTP by default; bind all interfaces inside the container.
ENV MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8089
EXPOSE 8089

CMD ["fileengine-mcp-http"]
