#!/usr/bin/env node
// `npx -y openajar`: runs `openajar mcp` from the openajar CLI installed on this computer (PATH, then
// ~/.local/bin/openajar), with stdio passed through and its exit code passed back. `npx openajar <args>`
// runs `openajar <args>`. When the CLI is not installed, it serves a minimal stdio MCP server that tells
// the agent (and, on stderr, the human) what to install, so a client never hangs on it.
// No dependencies: only Node's own modules.
"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const readline = require("node:readline");

const VERSION = require("../package.json").version;
const INSTALL_LINE = "curl -fsSL https://openajar.com/install.sh | sh";
const SETUP_LINE = "openajar setup";
const HELP_TOOL = "openajar_setup_help";
const DEFAULT_PROTOCOL = "2025-06-18";
// set on the CLI we start: if this launcher is ever started by it again, it must not look for itself
const GUARD = "OPENAJAR_NPM_LAUNCHER";

const NOT_INSTALLED =
  `openajar is not installed on this computer. To use it, install it in a terminal with \`${INSTALL_LINE}\`, ` +
  `then run \`${SETUP_LINE}\`. openajar gives the agent its own computer (a VM with a terminal and a real ` +
  "browser), so setup asks for a Hetzner Cloud token, or uses Docker running on this computer: one of the two " +
  "is needed. Then start the agent again. Docs: https://github.com/sabalys-ramunas/openajar";

function realpath(p) {
  try {
    return fs.realpathSync(p);
  } catch {
    return null;
  }
}

function isExecutable(p) {
  try {
    const st = fs.statSync(p);
    if (!st.isFile()) return false;
    fs.accessSync(p, fs.constants.X_OK);
    return true;
  } catch {
    return false;
  }
}

// The openajar CLI: the first `openajar` on PATH that is not this launcher (npx puts this one on PATH
// too), then ~/.local/bin/openajar, where the installer puts it.
function findCli(env = process.env) {
  if (env[GUARD]) return null;
  const self = realpath(__filename);
  const names = process.platform === "win32" ? ["openajar.exe", "openajar.cmd", "openajar"] : ["openajar"];
  const dirs = (env.PATH || "").split(path.delimiter).filter(Boolean);
  dirs.push(path.join(env.HOME || os.homedir(), ".local", "bin"));
  for (const dir of dirs) {
    for (const name of names) {
      const candidate = path.join(dir, name);
      if (!isExecutable(candidate)) continue;
      const real = realpath(candidate);
      if (real === self) continue; // this launcher, by any link (npx's .bin, a global install)
      return candidate;
    }
  }
  return null;
}

function log(message) {
  try {
    process.stderr.write(`openajar (npm): ${message}\n`);
  } catch {
    // stderr is gone: nothing more to say
  }
}

// What the minimal MCP server answers one message with (null: no answer, for a notification).
function answer(message, problem = NOT_INSTALLED) {
  if (message === null || typeof message !== "object" || Array.isArray(message)) {
    return { jsonrpc: "2.0", id: null, error: { code: -32600, message: "one JSON-RPC object per line" } };
  }
  const id = message.id;
  if (id === undefined || id === null) return null;
  const params = message.params && typeof message.params === "object" ? message.params : {};
  let result;
  switch (message.method) {
    case "initialize":
      result = {
        protocolVersion: typeof params.protocolVersion === "string" && params.protocolVersion
          ? params.protocolVersion : DEFAULT_PROTOCOL,
        capabilities: { tools: {} },
        serverInfo: { name: "openajar", title: "openajar (not installed)", version: VERSION },
        instructions: `${problem} Tell the human this, in these words, before anything else; the ${HELP_TOOL} ` +
          "tool says it again.",
      };
      break;
    case "ping":
      result = {};
      break;
    case "tools/list":
      result = {
        tools: [{
          name: HELP_TOOL,
          title: "How to set up openajar",
          description: `openajar is not ready on this computer. ${problem}`,
          inputSchema: { type: "object", properties: {} },
          annotations: { readOnlyHint: true, openWorldHint: false },
        }],
      };
      break;
    case "tools/call": {
      const ours = params.name === HELP_TOOL;
      const text = ours ? problem : `openajar has no tool ${JSON.stringify(params.name)} until it is installed. ${problem}`;
      result = { content: [{ type: "text", text }], isError: !ours };
      break;
    }
    case "prompts/list":
      result = { prompts: [] };
      break;
    case "resources/list":
      result = { resources: [] };
      break;
    case "resources/templates/list":
      result = { resourceTemplates: [] };
      break;
    default:
      return {
        jsonrpc: "2.0", id,
        error: { code: -32601, message: `openajar is not installed, so it has no ${message.method}. ${problem}` },
      };
  }
  return { jsonrpc: "2.0", id, result };
}

function serveHelp(input = process.stdin, output = process.stdout) {
  log(NOT_INSTALLED);
  const rl = readline.createInterface({ input, crlfDelay: Infinity });
  rl.on("line", (line) => {
    const text = line.trim();
    if (!text) return;
    let reply;
    try {
      reply = answer(JSON.parse(text));
    } catch {
      reply = { jsonrpc: "2.0", id: null, error: { code: -32700, message: "not JSON" } };
    }
    if (reply !== null) output.write(JSON.stringify(reply) + "\n");
  });
  rl.on("close", () => process.exit(0));
}

function runCli(cli, args) {
  const child = spawn(cli, args, { stdio: "inherit", env: { ...process.env, [GUARD]: "1" } });
  for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) {
    process.on(sig, () => {
      if (child.exitCode === null) child.kill(sig);
    });
  }
  child.on("error", (err) => {
    log(`could not run ${cli}: ${err.message}`);
    process.exit(127);
  });
  child.on("exit", (code, signal) => {
    if (signal) process.exit(128 + (os.constants.signals[signal] || 1));
    process.exit(code === null ? 1 : code);
  });
}

function main(argv = process.argv.slice(2)) {
  const args = argv.length ? argv : ["mcp"];
  const cli = findCli();
  if (cli) return runCli(cli, args);
  if (args[0] === "mcp") return serveHelp();
  if (args[0] === "--version" || args[0] === "-v") {
    process.stdout.write(`openajar launcher ${VERSION} (the openajar CLI is not installed)\n`);
    return process.exit(1);
  }
  log(NOT_INSTALLED);
  return process.exit(127);
}

module.exports = { answer, findCli, NOT_INSTALLED, INSTALL_LINE, HELP_TOOL };

if (require.main === module) main();
