"""
CPS Management Console — FastAPI Backend
=========================================
Bridges the web interface to the lab VMs via SSH:
  - OIDA command execution on Historian and OPC UA srv
  - Suricata rule management on the NGFW
  - Real-time log streaming via WebSocket

Targets are configured in targets.yml (deployed by Ansible).
SSH authentication uses the ed25519 keypair deployed by the cps-mgmt-console role.

SECURITY (read before changing the deployment)
-----------------------------------------------
This API has NO authentication and binds 0.0.0.0:8080. /api/oida/execute runs
an arbitrary command as the oida-runner user on a target, and /api/suricata/rules
writes free-form rules on the NGFW. This is acceptable ONLY because the console
lives on the isolated management VLAN (99) of a throwaway workshop range. Do not
expose this port beyond that VLAN, and do not reuse this backend on a real network.
"""
import os
import asyncio
import logging
import yaml
from pathlib import Path
from typing import Optional

import asyncssh
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cps-console")

# --- Configuration ---

TARGETS_FILE = os.getenv("TARGETS_FILE", "targets.yml")
SSH_KEY = os.getenv("SSH_KEY", os.path.expanduser("~/.ssh/id_ed25519"))

def load_targets():
    with open(TARGETS_FILE) as f:
        return yaml.safe_load(f)

TARGETS = load_targets()

def range_octet() -> str:
    """Second octet of the range, read from a real target IP (10.<octet>.x.x).
    The frontend uses this to resolve the '10.x.' placeholders in attack
    commands before they are executed, so IPs are always correct."""
    try:
        return TARGETS["historian"]["host"].split(".")[1]
    except Exception:
        return "x"

# --- FastAPI app ---

app = FastAPI(
    title="CPS Management Console",
    description="OIDA orchestration + Suricata rule management",
    version="1.0.0",
)

# Serve frontend
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(static_dir / "index.html"))


# --- Models ---

class OidaRequest(BaseModel):
    target: str  # "historian" or "opcua_srv"
    command: str  # e.g. "oida s7 -i 10.x.10.11"
    timeout: int = 30

class OidaResponse(BaseModel):
    status: str
    output: str
    exit_code: int

class RulesRequest(BaseModel):
    probe: str  # "s1" or "s2"
    rules: str  # Suricata rule text
    append: bool = False  # True = append, False = replace

class RulesResponse(BaseModel):
    status: str
    rules_count: int
    reload_output: str
    errors: list[str]

class StatusResponse(BaseModel):
    historian: bool
    opcua_srv: bool
    ngfw: bool
    range_octet: str  # second octet of the range (10.<octet>.x.x), for the UI


# --- SSH helpers ---

async def ssh_exec(target_name: str, command: str, timeout: int = 30) -> tuple[str, int]:
    """Execute a command on a target VM via SSH. Returns (output, exit_code)."""
    target = TARGETS.get(target_name)
    if not target:
        raise HTTPException(status_code=400, detail=f"Unknown target: {target_name}")

    try:
        async with asyncssh.connect(
            target["host"],
            port=target.get("port", 22),
            username=target["user"],
            client_keys=[SSH_KEY],
            known_hosts=None,
            connect_timeout=5,
        ) as conn:
            result = await asyncio.wait_for(
                conn.run(command, check=False),
                timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return output.strip(), result.exit_status or 0
    except asyncio.TimeoutError:
        return f"[!] Command timed out after {timeout}s", 124
    except asyncssh.Error as e:
        return f"[!] SSH error: {e}", 255
    except OSError as e:
        return f"[!] Connection failed: {e}", 255

async def ssh_check(target_name: str) -> bool:
    """Check if a target is reachable via SSH."""
    try:
        output, code = await ssh_exec(target_name, "echo ok", timeout=5)
        return code == 0 and "ok" in output
    except Exception:
        return False


# --- API Endpoints ---

@app.post("/api/oida/execute", response_model=OidaResponse)
async def execute_oida(req: OidaRequest):
    """Execute an OIDA command on Historian or OPC UA srv via SSH."""
    if req.target not in ("historian", "opcua_srv"):
        raise HTTPException(400, "Target must be 'historian' or 'opcua_srv'")

    # Resolve the '10.x.' placeholder to the real range octet, so a command
    # is always aimed at a valid IP even if the frontend did not substitute.
    command = req.command.replace("10.x.", f"10.{range_octet()}.")
    log.info(f"OIDA exec on {req.target}: {command}")
    output, exit_code = await ssh_exec(req.target, command, req.timeout)

    return OidaResponse(
        status="success" if exit_code == 0 else "error",
        output=output,
        exit_code=exit_code,
    )

@app.post("/api/suricata/rules", response_model=RulesResponse)
async def push_rules(req: RulesRequest):
    """Push Suricata rules to the NGFW and reload the engine."""
    if req.probe not in ("s1", "s2"):
        raise HTTPException(400, "Probe must be 's1' or 's2'")

    rules_file = f"/etc/suricata/rules/custom_{req.probe}.rules"
    errors = []

    # Write rules to NGFW. The suricata-mgmt user owns the rules files
    # (see cps-ngfw-suricata role), so no sudo is needed here — only the
    # reload below is privileged. Use `tee -a` for a real append.
    tee_flag = "-a" if req.append else ""
    escaped_rules = req.rules.replace("'", "'\\''")
    write_cmd = f"echo '{escaped_rules}' | tee {tee_flag} {rules_file} > /dev/null"

    output, code = await ssh_exec("ngfw", write_cmd)
    if code != 0:
        errors.append(f"Write failed: {output}")

    # Count rules
    count_output, _ = await ssh_exec("ngfw", f"grep -c '^alert\\|^drop' {rules_file} 2>/dev/null || echo 0")
    try:
        rules_count = int(count_output.strip())
    except ValueError:
        rules_count = 0

    # Reload Suricata
    reload_cmd = "sudo suricatasc -c reload-rules /run/suricata-{}.socket 2>&1 || sudo systemctl restart suricata-{}".format(
        req.probe, req.probe
    )
    reload_output, reload_code = await ssh_exec("ngfw", reload_cmd)
    if reload_code != 0:
        errors.append(f"Reload failed: {reload_output}")

    return RulesResponse(
        status="reloaded" if not errors else "partial",
        rules_count=rules_count,
        reload_output=reload_output,
        errors=errors,
    )

@app.get("/api/status", response_model=StatusResponse)
async def check_status():
    """Check SSH connectivity to all target VMs."""
    results = await asyncio.gather(
        ssh_check("historian"),
        ssh_check("opcua_srv"),
        ssh_check("ngfw"),
    )
    return StatusResponse(
        historian=results[0],
        opcua_srv=results[1],
        ngfw=results[2],
        range_octet=range_octet(),
    )

@app.get("/api/suricata/rules/{probe}")
async def get_rules(probe: str):
    """Read current custom rules for a probe."""
    if probe not in ("s1", "s2"):
        raise HTTPException(400, "Probe must be 's1' or 's2'")

    rules_file = f"/etc/suricata/rules/custom_{probe}.rules"
    output, code = await ssh_exec("ngfw", f"cat {rules_file} 2>/dev/null || echo ''")
    return {"probe": probe, "rules": output}


# --- WebSocket: real-time log streaming ---

@app.websocket("/ws/logs/{probe}")
async def stream_logs(ws: WebSocket, probe: str):
    """Stream Suricata fast.log in real-time from the NGFW via SSH tail -f."""
    if probe not in ("s1", "s2"):
        await ws.close(code=4000, reason="Invalid probe")
        return

    await ws.accept()
    log.info(f"WebSocket connected for probe {probe}")

    target = TARGETS.get("ngfw")
    if not target:
        await ws.send_text("[!] NGFW target not configured")
        await ws.close()
        return

    log_file = f"/var/log/suricata/{probe}/fast.log"

    try:
        async with asyncssh.connect(
            target["host"],
            port=target.get("port", 22),
            username=target["user"],
            client_keys=[SSH_KEY],
            known_hosts=None,
        ) as conn:
            # Start tail -f in a process
            process = await conn.create_process(f"tail -n 50 -f {log_file}")

            # Read stdout and forward to WebSocket
            async def read_output():
                async for line in process.stdout:
                    line = line.strip()
                    if line:
                        await ws.send_text(line)

            # Listen for WebSocket close
            async def listen_ws():
                try:
                    while True:
                        msg = await ws.receive_text()
                        # Client can send "ping" to keep alive
                except WebSocketDisconnect:
                    pass

            # Run both concurrently, stop when either ends
            done, pending = await asyncio.wait(
                [asyncio.create_task(read_output()), asyncio.create_task(listen_ws())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            process.kill()

    except asyncssh.Error as e:
        try:
            await ws.send_text(f"[!] SSH error: {e}")
        except Exception:
            pass
    except WebSocketDisconnect:
        pass
    finally:
        log.info(f"WebSocket disconnected for probe {probe}")


# --- Health ---

@app.get("/health")
async def health():
    return {"status": "ok", "service": "cps-console"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
