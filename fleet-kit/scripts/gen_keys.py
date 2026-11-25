#!/usr/bin/env python3
"""Issue Fleet Kit API keys and their hashes (B2). Python stdlib only.

The Hub stores and compares only key HASHES (app/identity/keyhash.py). This
script is where keys are born: it prints or writes the key for the CLIENT
(X-API-Key, MCP host env, FLEET_* variables) and the hash for the HUB
(AI_FLEET_* settings). Keys are 32 random bytes from `secrets`, URL-safe
base64 (43 characters, 256 bits).

  python3 scripts/gen_keys.py new
      One fresh key and its hash (tenant, agent or operator key).
  python3 scripts/gen_keys.py hash < key.txt
      Hash a key issued elsewhere (read from stdin, never from argv, so it
      does not end up in shell history or `ps`). Keys under 24 characters
      are refused.
  python3 scripts/gen_keys.py demo --shell
      A fresh demo key set as `export` lines (used by demo.sh).
  python3 scripts/gen_keys.py demo --env-file .demo-keys.env --mcp-host mcp-host.json
      Same, written to files (mode 0600, both gitignored): source the env
      file before starting the Hub; paste mcp-host.json into the MCP host.

With AI_FLEET_KEY_PEPPER set (>= 32 characters), hashes are hmac-sha256 and
the Hub must run with the same pepper. Without the script (sha256 form):

  key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')
  printf 'sha256:%s\\n' "$(printf %s "$key" | openssl dgst -sha256 -r | cut -d' ' -f1)"
  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # key only
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_BYTES = 32
MIN_KEY_LENGTH = 24
DEMO_CLIENT_VARS = (
    "FLEET_API_KEY",
    "FLEET_OTHER_TENANT_KEY",
    "FLEET_AGENT_A_KEY",
    "FLEET_AGENT_B_KEY",
)
MCP_HOST_TEMPLATE = ROOT / "mcp-host.example.json"
ROOT_PLACEHOLDER = "/ABS/PATH/TO/fleet-kit"


def _load_keyhash():
    """app/identity/keyhash.py loaded by path: one hashing implementation for
    Hub and script, without importing the app package (FastAPI, settings)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "fleet_kit_keyhash", ROOT / "app" / "identity" / "keyhash.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


keyhash = _load_keyhash()


def new_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def pepper_from_env(env=os.environ) -> str:
    pepper = env.get(keyhash.PEPPER_ENV, "")
    if pepper and len(pepper) < keyhash.MIN_PEPPER_LENGTH:
        raise SystemExit(f"{keyhash.PEPPER_ENV} must be at least {keyhash.MIN_PEPPER_LENGTH} characters")
    return pepper


def demo_keys(env=os.environ) -> dict:
    """Client-side demo keys: a preset FLEET_* value is kept (it must be at
    least MIN_KEY_LENGTH characters), every other one is fresh and random."""
    keys = {}
    for var in DEMO_CLIENT_VARS:
        preset = env.get(var, "")
        if preset and len(preset) < MIN_KEY_LENGTH:
            raise SystemExit(f"{var} is set but shorter than {MIN_KEY_LENGTH} characters; unset it to get a random key")
        keys[var] = preset or new_key()
    if len(set(keys.values())) != len(keys):
        raise SystemExit("demo keys must all differ")
    return keys


def demo_server_env(keys: dict, pepper: str = "") -> dict:
    """The Hub side of a demo key set: hashes only."""

    def h(var):
        return keyhash.hash_key(keys[var], pepper)

    return {
        "AI_FLEET_API_KEY": h("FLEET_API_KEY"),
        "AI_FLEET_TENANT_API_KEYS": json.dumps({
            h("FLEET_API_KEY"): "demo-tenant",
            h("FLEET_OTHER_TENANT_KEY"): "other-tenant",
        }),
        "AI_FLEET_AGENT_API_KEYS": json.dumps({
            h("FLEET_AGENT_A_KEY"): {"agent_id": "orchestrator", "tenant_id": "demo-tenant"},
            h("FLEET_AGENT_B_KEY"): {"agent_id": "worker", "tenant_id": "demo-tenant"},
        }),
    }


def export_lines(values: dict) -> str:
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in values.items())


def render_mcp_host(template: str, keys: dict) -> str:
    """mcp-host.example.json with its ${FLEET_AGENT_*_KEY} placeholders
    replaced by the demo agent keys and the fleet-kit path filled in."""
    out = template.replace(ROOT_PLACEHOLDER, str(ROOT))
    for var in ("FLEET_AGENT_A_KEY", "FLEET_AGENT_B_KEY"):
        placeholder = "${" + var + "}"
        if placeholder not in out:
            raise SystemExit(f"{MCP_HOST_TEMPLATE.name} has no {placeholder} placeholder")
        out = out.replace(placeholder, keys[var])
    json.loads(out)  # still valid JSON
    return out


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)


def _cmd_new(args) -> None:
    key = new_key()
    print(f"key:  {key}")
    print(f"hash: {keyhash.hash_key(key, pepper_from_env())}")
    print("Give the key to the client once; configure only the hash on the Hub.", file=sys.stderr)


def _cmd_hash(args) -> None:
    key = sys.stdin.readline().rstrip("\r\n")
    if len(key) < MIN_KEY_LENGTH:
        raise SystemExit(f"key is shorter than {MIN_KEY_LENGTH} characters; issue a new one with: gen_keys.py new")
    print(keyhash.hash_key(key, pepper_from_env()))


def _cmd_demo(args) -> None:
    keys = demo_keys()
    values = dict(keys)
    values.update(demo_server_env(keys, pepper_from_env()))
    if args.shell:
        sys.stdout.write(export_lines(values))
    if args.env_file:
        _write_private(Path(args.env_file), export_lines(values))
        print(f"wrote {args.env_file} (0600); load it with: set -a; . ./{args.env_file}; set +a", file=sys.stderr)
    if args.mcp_host:
        _write_private(Path(args.mcp_host), render_mcp_host(MCP_HOST_TEMPLATE.read_text(), keys))
        print(f"wrote {args.mcp_host} (0600); restart the Hub with the matching env file", file=sys.stderr)
    if not (args.shell or args.env_file or args.mcp_host):
        raise SystemExit("demo: pass --shell, --env-file and/or --mcp-host")


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description="Issue Fleet Kit API keys and their hashes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new", help="one fresh key and its hash")
    sub.add_parser("hash", help="hash a key read from stdin")
    demo = sub.add_parser("demo", help="a fresh demo key set")
    demo.add_argument("--shell", action="store_true", help="print export lines")
    demo.add_argument("--env-file", help="write export lines to this file (0600)")
    demo.add_argument("--mcp-host", help="write an MCP host config with the agent keys (0600)")
    args = parser.parse_args(argv)
    {"new": _cmd_new, "hash": _cmd_hash, "demo": _cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    main()
