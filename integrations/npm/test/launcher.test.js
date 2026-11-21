// node --test test/: the launcher with no openajar CLI, with a fake one, and never itself.
"use strict";

const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const LAUNCHER = path.join(__dirname, "..", "bin", "openajar.js");
const { answer, findCli, INSTALL_LINE, HELP_TOOL } = require(LAUNCHER);

function scratch() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "openajar-npm-"));
}

// The launcher in a computer with only `dirs` on PATH and an empty HOME (no ~/.local/bin/openajar).
function run(args, { input = "", dirs = [], home = scratch() } = {}) {
  const env = { PATH: [...dirs, "/usr/bin", "/bin"].join(path.delimiter), HOME: home };
  return spawnSync(process.execPath, [LAUNCHER, ...args], { input, env, encoding: "utf8", timeout: 20000 });
}

function fakeCli(body) {
  const dir = scratch();
  const file = path.join(dir, "openajar");
  fs.writeFileSync(file, `#!/bin/sh\n${body}\n`, { mode: 0o755 });
  return dir;
}

const INIT = JSON.stringify({
  jsonrpc: "2.0", id: 1, method: "initialize",
  params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "t", version: "1" } },
});

test("without the CLI it serves MCP that says what to install, and says it on stderr", () => {
  const input = [
    INIT,
    JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" }),
    JSON.stringify({ jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: HELP_TOOL, arguments: {} } }),
    "not json",
    "",
  ].join("\n");
  const proc = run([], { input });
  assert.equal(proc.status, 0, proc.stderr);
  const replies = proc.stdout.trim().split("\n").map((line) => JSON.parse(line));
  assert.deepEqual(replies.map((r) => r.id), [1, 2, 3, null]);
  assert.equal(replies[0].result.protocolVersion, "2025-03-26");
  assert.equal(replies[0].result.serverInfo.name, "openajar");
  assert.ok(replies[0].result.instructions.includes(INSTALL_LINE));
  assert.deepEqual(replies[1].result.tools.map((t) => t.name), [HELP_TOOL]);
  assert.ok(replies[2].result.content[0].text.includes("Docker"));
  assert.equal(replies[3].error.code, -32700);
  assert.ok(proc.stderr.includes(INSTALL_LINE));
  assert.ok(!(proc.stdout + proc.stderr).includes("\u2014"));
});

test("without the CLI, other arguments fail with the install line", () => {
  const proc = run(["local", "status"]);
  assert.equal(proc.status, 127);
  assert.equal(proc.stdout, "");
  assert.ok(proc.stderr.includes(INSTALL_LINE));
});

test("with no arguments it runs `openajar mcp`, passing stdio and the exit code", () => {
  const dir = fakeCli('echo "args:$*"; read line; echo "got:$line"; echo err >&2; exit 7');
  const proc = run([], { dirs: [dir], input: "hello\n" });
  assert.equal(proc.status, 7);
  assert.equal(proc.stdout, "args:mcp\ngot:hello\n");
  assert.equal(proc.stderr, "err\n");
});

test("arguments go to the CLI as they are", () => {
  const dir = fakeCli('printf "%s|" "$@"; exit 0');
  const proc = run(["local", "status", "--json"], { dirs: [dir] });
  assert.equal(proc.status, 0);
  assert.equal(proc.stdout, "local|status|--json|");
});

test("~/.local/bin/openajar is found when it is not on PATH", () => {
  const home = scratch();
  fs.mkdirSync(path.join(home, ".local", "bin"), { recursive: true });
  fs.writeFileSync(path.join(home, ".local", "bin", "openajar"), "#!/bin/sh\necho local-bin $*\n", { mode: 0o755 });
  const proc = run(["--version"], { home });
  assert.equal(proc.status, 0);
  assert.equal(proc.stdout, "local-bin --version\n");
});

test("it never runs itself, by a link on PATH or when the CLI started it", () => {
  const dir = scratch();
  fs.symlinkSync(LAUNCHER, path.join(dir, "openajar"));
  const home = scratch();
  assert.equal(findCli({ PATH: dir, HOME: home }), null);
  const other = fakeCli("exit 0");
  assert.equal(findCli({ PATH: other, HOME: home }), path.join(other, "openajar"));
  assert.equal(findCli({ PATH: other, HOME: home, OPENAJAR_NPM_LAUNCHER: "1" }), null);
});

test("notifications get no answer and unknown methods say so", () => {
  assert.equal(answer({ jsonrpc: "2.0", method: "notifications/cancelled" }), null);
  assert.equal(answer({ jsonrpc: "2.0", id: 9, method: "completion/complete" }).error.code, -32601);
  assert.equal(answer([1]).error.code, -32600);
});
