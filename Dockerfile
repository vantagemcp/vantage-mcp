FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src

RUN pip install --no-cache-dir .

# stdio by default - what registry scanners expect: a plain MCP process
# to pipe an `initialize` request into over stdin. The hosted network
# mode (--http, behind our own reverse proxy) is a deliberate opt-in,
# not the container default.
ENTRYPOINT ["python", "-m", "vantage_mcp.server"]
