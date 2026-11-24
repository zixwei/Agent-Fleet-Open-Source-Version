#!/usr/bin/env python3
"""Formal T01 live acceptance: two independent agent processes, real HTTP.

The acceptance runner (run_t01_t15.py) can only check routing metadata:
it hardcodes recipient_receipt_observed=False, so its T01 verdict can
never be PASS. This script is the receipt evidence. Two agent processes
run as separate OS processes, each holding ONLY its own agent credential
(never the other agent's key, never the tenant key). They exchange N
rounds over real HTTP against a real uvicorn hub:

  round r: A sends a request (explicit recipient B)
           -> B receipts it with B's own credential
           -> B replies with NO recipient field (implicit routing -> A)
           -> A receipts it with A's own credential

PASS requires every round to complete on both sides: 2N messages sent,
2N receipts recorded, audit chain verified afterwards. Any failed round
aborts that agent immediately (fail fast, no filler rounds).

Run from the prototype checkout (venv with frozen requirements):

  .venv/bin/python prototype/tests/t01_live_two_process.py \\
      --rounds 100 --output ~/workspace/ai-fleet-integration/t01_live_two_process_results.json

All credentials below are synthetic test-only values.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

TENANT_KEY = "live-test-tenant-key"
AGENT_KEYS = {
    "agent-a": "live-test-key-a",
    "agent-b": "live-test-key-b",
}
TENANT_MAP = {"live-test-tenant-key": "tenant-a"}
AGENT_MAP = {
    "live-test-key-a": {"agent_id": "agent-a", "tenant_id": "tenant-a"},
    "live-test-key-b": {"agent_id": "agent-b", "tenant_id": "tenant-a"},
}
POLL_INTERVAL = 0.05
STEP_TIMEOUT = 30.0


def make_client() -> httpx.Client:
    # trust_env=False: the sandbox sets proxy variables httpx cannot
    # parse; this test only talks to 127.0.0.1 and must not use a proxy.
    return httpx.Client(trust_env=False, timeout=10.0)


def log_line(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_health(base_url: str, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    with make_client() as client:
        while time.monotonic() < deadline:
            try:
                r = client.get(f"{base_url}/health")
                if r.status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
    return False


def list_messages(base_url: str, task_id: str, key: str) -> list[dict]:
    with make_client() as client:
        r = client.get(
            f"{base_url}/tasks/{task_id}/messages",
            headers={"X-API-Key": key},
        )
        r.raise_for_status()
        return r.json()


def wait_for(
    description: str,
    predicate,
    base_url: str,
    task_id: str,
    key: str,
    timeout: float = STEP_TIMEOUT,
):
    deadline = time.monotonic() + timeout
    while True:
        messages = list_messages(base_url, task_id, key)
        found = predicate(messages)
        if found is not None:
            return found
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {description}")
        time.sleep(POLL_INTERVAL)


def run_agent(role: str, base_url: str, task_id: str, rounds: int, log_path: Path) -> int:
    agent_id = role
    key = os.environ["AGENT_KEY"]
    other = "agent-b" if agent_id == "agent-a" else "agent-a"
    client = make_client()
    try:
        for r in range(1, rounds + 1):
            record: dict = {"role": agent_id, "round": r, "status": "FAIL"}
            if agent_id == "agent-a":
                # A sends the request (explicit recipient B).
                resp = client.post(
                    f"{base_url}/tasks/{task_id}/messages",
                    json={"sender_agent_id": "agent-a", "recipient_agent_id": "agent-b",
                          "event_type": "test.request", "payload": {"round": r}},
                    headers={"X-API-Key": key},
                )
                assert resp.status_code == 200, f"send request: {resp.status_code} {resp.text}"
                req = resp.json()["message"]
                assert req["recipient_agent_id"] == "agent-b", req
                record["request_message_id"] = req["message_id"]
                # Wait until B receipted the request.
                wait_for(
                    f"receipt of request round {r}",
                    lambda msgs, mid=req["message_id"]: next(
                        (m for m in msgs if m["message_id"] == mid and m.get("receipt")), None),
                    base_url, task_id, key,
                )
                record["request_receipted_by"] = "agent-b"
                # Wait for B's reply, then receipt it with A's own credential.
                reply = wait_for(
                    f"reply round {r}",
                    lambda msgs: next(
                        (m for m in msgs
                         if m["event_type"] == "test.reply"
                         and m["payload"].get("round") == r
                         and m["recipient_agent_id"] == "agent-a"
                         and m.get("receipt") is None), None),
                    base_url, task_id, key,
                )
                assert reply["sender_agent_id"] == "agent-b", reply
                record["reply_message_id"] = reply["message_id"]
                rr = client.post(
                    f"{base_url}/tasks/{task_id}/messages/{reply['message_id']}/receipt",
                    json={},
                    headers={"X-API-Key": key},
                )
                assert rr.status_code == 200, f"receipt reply: {rr.status_code} {rr.text}"
                assert rr.json()["stored"] is True, rr.json()
                record["reply_receipt_stored"] = True
            else:
                # B waits for A's request, receipts it, then replies.
                req = wait_for(
                    f"request round {r}",
                    lambda msgs: next(
                        (m for m in msgs
                         if m["event_type"] == "test.request"
                         and m["payload"].get("round") == r
                         and m["recipient_agent_id"] == "agent-b"
                         and m.get("receipt") is None), None),
                    base_url, task_id, key,
                )
                assert req["sender_agent_id"] == "agent-a", req
                record["request_message_id"] = req["message_id"]
                rr = client.post(
                    f"{base_url}/tasks/{task_id}/messages/{req['message_id']}/receipt",
                    json={},
                    headers={"X-API-Key": key},
                )
                assert rr.status_code == 200, f"receipt request: {rr.status_code} {rr.text}"
                assert rr.json()["stored"] is True, rr.json()
                record["request_receipt_stored"] = True
                # B replies with NO recipient field: implicit routing -> A.
                resp = client.post(
                    f"{base_url}/tasks/{task_id}/messages",
                    json={"sender_agent_id": "agent-b",
                          "event_type": "test.reply", "payload": {"round": r}},
                    headers={"X-API-Key": key},
                )
                assert resp.status_code == 200, f"send reply: {resp.status_code} {resp.text}"
                rep = resp.json()["message"]
                assert rep["recipient_agent_id"] == "agent-a", rep
                record["reply_message_id"] = rep["message_id"]
                # Wait until A receipted the reply.
                wait_for(
                    f"receipt of reply round {r}",
                    lambda msgs, mid=rep["message_id"]: next(
                        (m for m in msgs if m["message_id"] == mid and m.get("receipt")), None),
                    base_url, task_id, key,
                )
                record["reply_receipted_by"] = "agent-a"
            record["status"] = "PASS"
            log_line(log_path, record)
        return 0
    except Exception as exc:  # noqa: BLE001 - fail fast, record, exit nonzero
        record["status"] = "FAIL"
        record["error"] = f"{type(exc).__name__}: {exc}"
        log_line(log_path, record)
        return 1


def run_driver(rounds: int, output: Path) -> int:
    prototype_dir = Path(__file__).resolve().parent.parent
    started = datetime.now(timezone.utc).isoformat()
    result: dict = {
        "test": "T01 formal live two-process",
        "rounds_target": rounds,
        "started_at_utc": started,
        "status": "FAIL",
    }
    with tempfile.TemporaryDirectory(prefix="ai-fleet-t01-live-") as tmp:
        tmpdir = Path(tmp)
        db_path = tmpdir / "live.db"
        hub_log = open(tmpdir / "hub.log", "wb")
        hub_env = {
            **os.environ,
            "AI_FLEET_DB": f"sqlite:///{db_path}",
            "AI_FLEET_API_KEY": TENANT_KEY,
            "AI_FLEET_TENANT_API_KEYS": json.dumps(TENANT_MAP),
            "AI_FLEET_AGENT_API_KEYS": json.dumps(AGENT_MAP),
            "AI_FLEET_TENANT": "tenant-a",
        }
        port = free_port()
        base_url = f"http://127.0.0.1:{port}"
        hub = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(prototype_dir), env=hub_env,
            stdout=hub_log, stderr=subprocess.STDOUT,
        )
        result["hub"] = {"base_url": base_url, "port": port}
        try:
            if not wait_for_health(base_url):
                result["error"] = "hub did not become healthy"
                return finish(result, output, 1)
            tenant_headers = {"X-API-Key": TENANT_KEY}
            http = make_client()
            for agent_id in ("agent-a", "agent-b"):
                r = http.post(
                    f"{base_url}/agents",
                    json={"agent_id": agent_id, "name": agent_id,
                          "endpoint": f"http://{agent_id}.local"},
                    headers=tenant_headers,
                )
                assert r.status_code == 200, r.text
                r = http.post(f"{base_url}/agents/{agent_id}/approve",
                              headers=tenant_headers)
                assert r.json()["status"] == "Active", r.text
            r = http.post(
                f"{base_url}/tasks",
                json={"agent_id": "agent-b", "title": "T01 live two-process"},
                headers=tenant_headers,
            )
            assert r.status_code == 200, r.text
            task_id = r.json()["task_id"]
            result["task_id"] = task_id

            logs = {}
            procs = {}
            for role in ("agent-a", "agent-b"):
                log_path = tmpdir / f"{role}.jsonl"
                logs[role] = log_path
                env = {**os.environ, "AGENT_KEY": AGENT_KEYS[role]}
                procs[role] = subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()),
                     "--role", role, "--base-url", base_url,
                     "--task-id", task_id, "--rounds", str(rounds),
                     "--log", str(log_path)],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            exits = {}
            for role, proc in procs.items():
                try:
                    exits[role] = proc.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    exits[role] = "timeout"
            result["agent_exits"] = {k: str(v) for k, v in exits.items()}

            records: dict[int, dict] = {}
            for role in ("agent-a", "agent-b"):
                for line in logs[role].read_text(encoding="utf-8").splitlines():
                    rec = json.loads(line)
                    records.setdefault(rec["round"], {})[role] = rec
            merged = []
            passed = 0
            for r in range(1, rounds + 1):
                entry = {"round": r,
                         "agent-a": records.get(r, {}).get("agent-a"),
                         "agent-b": records.get(r, {}).get("agent-b")}
                ok = (entry["agent-a"] and entry["agent-a"].get("status") == "PASS"
                      and entry["agent-b"] and entry["agent-b"].get("status") == "PASS")
                entry["status"] = "PASS" if ok else "FAIL"
                if ok:
                    passed += 1
                merged.append(entry)
            result["rounds"] = merged
            result["rounds_passed"] = passed
            result["rounds_failed"] = rounds - passed

            # Final state checks against the hub (tenant key).
            final = http.get(f"{base_url}/tasks/{task_id}/messages",
                             headers=tenant_headers).json()
            result["final_message_list"] = {
                "count": len(final),
                "all_receipted": all(m.get("receipt") for m in final),
                "requests": sum(1 for m in final if m["event_type"] == "test.request"),
                "replies": sum(1 for m in final if m["event_type"] == "test.reply"),
            }
            result["messages_sent"] = len(final)
            result["receipts_recorded"] = sum(1 for m in final if m.get("receipt"))
            verify = http.get(f"{base_url}/audit/verify",
                              headers=tenant_headers).json()
            result["audit_verify"] = verify
            result["status"] = (
                "PASS"
                if passed == rounds and exits.get("agent-a") == 0 and exits.get("agent-b") == 0
                and result["final_message_list"]["count"] == 2 * rounds
                and result["final_message_list"]["all_receipted"]
                and verify.get("ok") is True
                else "FAIL"
            )
        finally:
            hub.terminate()
            try:
                hub.wait(timeout=10)
            except subprocess.TimeoutExpired:
                hub.kill()
            hub_log.close()
    result["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return finish(result, output, 0 if result["status"] == "PASS" else 1)


def finish(result: dict, output: Path, code: int) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(output), "status": result["status"],
        "rounds_passed": result.get("rounds_passed"),
        "rounds_target": result.get("rounds_target"),
        "messages_sent": result.get("messages_sent"),
        "receipts_recorded": result.get("receipts_recorded"),
        "audit_verify": result.get("audit_verify"),
    }, ensure_ascii=False, indent=2))
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", default="driver",
                        choices=["driver", "agent-a", "agent-b"])
    parser.add_argument("--base-url", default="")
    parser.add_argument("--task-id", default="")
    parser.add_argument("--rounds", type=int, default=100)
    parser.add_argument("--log", default="")
    parser.add_argument("--output", default="t01_live_two_process_results.json")
    args = parser.parse_args()
    if args.role in ("agent-a", "agent-b"):
        return run_agent(args.role, args.base_url, args.task_id, args.rounds,
                         Path(args.log))
    return run_driver(args.rounds, Path(args.output))


if __name__ == "__main__":
    raise SystemExit(main())
