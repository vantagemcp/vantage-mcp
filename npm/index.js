#!/usr/bin/env node
// Vantage runs as a hosted MCP server at https://vantagemcp.dev/mcp. This
// package is only the bridge for clients that start servers as a local command
// (npx): it hands the connection to mcp-remote. With VANTAGE_API_KEY set the key
// is sent as a bearer token; without it, mcp-remote opens the sign-in page.
const { spawn } = require("node:child_process");
const path = require("node:path");

const url = process.env.VANTAGE_MCP_URL || "https://vantagemcp.dev/mcp";
const proxy = path.join(path.dirname(require.resolve("mcp-remote/package.json")), "dist", "proxy.js");
const args = [proxy, url];
if (process.env.VANTAGE_API_KEY) {
  // mcp-remote's documented pattern: no space after the colon, value from env,
  // so the key never appears in the process list.
  args.push("--header", "Authorization:${VANTAGE_AUTH_HEADER}");
}
const env = { ...process.env };
if (process.env.VANTAGE_API_KEY) env.VANTAGE_AUTH_HEADER = `Bearer ${process.env.VANTAGE_API_KEY}`;

const child = spawn(process.execPath, [...args, ...process.argv.slice(2)], { stdio: "inherit", env });
// Pass stop signals on, so stopping this bridge never leaves mcp-remote running.
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, () => child.kill(sig));
child.on("exit", (code, signal) => process.exit(signal ? 1 : code ?? 0));
