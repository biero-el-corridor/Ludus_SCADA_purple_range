# CPS-Lab — ICS/OT Cyber Range on Ludus

A realistic, deployable ICS/OT cyber-physical environment built on [Ludus](https://docs.ludus.cloud). Simulates a 3-layer Purdue model industrial network with live protocol simulators (S7comm, Modbus TCP, OPC UA), a central NGFW running Suricata IDS, and a web-based attack/defense console powered by [OIDA](https://getoida.dev).

> **Lab security notice:** this range is intentionally insecure (weak SSH password, SUPERUSER PostgreSQL, a committed disposable SSH keypair in `ludus/keys/`). It is built for an isolated workshop range. Never expose it to a production network and never reuse its credentials or keys.

Designed for collaborative workshops where participants walk through a complete ICS kill chain (reconnaissance, lateral movement, exploitation) while writing and testing Suricata detection rules in real time.

## Architecture

```
 VLAN 99 — Management            VLAN 30 — L3 Supervision
┌──────────────┐                 ┌──────────────┐  ┌──────────────┐
│   Console    │                 │  Historian   │  │     EWS      │
│  FastAPI UI  │                 │  PostgreSQL  │  │   (admin)    │
│  :8080       │                 │  OIDA-ready  │  │              │
└──────┬───────┘                 └──────┬───────┘  └──────┬───────┘
       │                                │                  │
═══════╪════════════════════════════════╪══════════════════╪═══════
       │         NGFW (Ludus Router)    │                  │
       │    ┌───────────────────────┐   │                  │
       │    │  Suricata S1: eth0.30 │───┘                  │
       │    │  Suricata S2: eth0.10 │                      │
       │    │  Firewall + Routing   │──────────────────────┘
       │    └───────────┬───────────┘
       │                │
═══════╪════════════════╪═════════════════════════════════════════
       │                │
       │     VLAN 20 — L2 Control           VLAN 10 — L1 Field
       │    ┌──────────────┐  ┌─────────┐  ┌──────────────────┐
       │    │  OPC UA srv  │  │   IHM   │  │       PLC        │
       │    │  :4840       │  │ polling  │  │ Modbus :502      │
       │    │  OIDA-ready  │  │ client   │  │ S7comm :102      │
       │    │  lowpriv SSH │  │          │  │ OPC UA :4840     │
       │    └──────────────┘  └─────────┘  └──────────────────┘
       │
  SSH orchestration (asyncssh)
```

7 VMs total, ~16 vCPU / 18 GB RAM comfortable (9 vCPU / 10 GB minimum).

## Kill Chain Phases

| Phase | From | To | Action | Probe |
|-------|------|----|--------|-------|
| 1 | (oral) | Historian | Initial compromise via pg_execute | — |
| 2 | Historian (L3) | OPC UA srv (L2) | OIDA recon (opcua discover, browse) | S1 |
| 3 | Historian (L3) | OPC UA srv (L2) | SSH pivot (lowpriv) | S1 |
| 4 | OPC UA srv (L2) | PLC (L1) | OIDA recon (s7 -i, list-blocks) | S2 |
| 5 | OPC UA srv (L2) | PLC (L1) | Exploitation (s7 stop, write, modbus write-coil) | S2 |

## Repository Structure

```
cps-lab/
├── README.md
├── LICENSE
├── .gitignore
│
├── ludus/                          # Ludus deployment
│   ├── range-config.yml            #   Main config (7 VMs, VLANs, firewall rules)
│   └── roles/                      #   Ansible roles (auto-provisioning)
│       ├── cps-ngfw-suricata/      #     Suricata IDS on NGFW (2 instances: S1, S2)
│       ├── cps-postgres-vuln/      #     PostgreSQL with SUPERUSER (pg_execute)
│       ├── cps-oida-ready/         #     OIDA tooling + SSH key (Historian, OPC UA srv)
│       ├── cps-opcua-server/       #     OPC UA mock server (Shopfloor namespace)
│       ├── cps-opcua-field/        #     OPC UA field-level server (raw I/O)
│       ├── cps-s7comm/             #     S7comm mock server (snap7, CPU 315-2 PN/DP)
│       ├── cps-modbus/             #     Modbus TCP mock server (pymodbus)
│       ├── cps-ssh-lowpriv/        #     Weak SSH user for lateral movement
│       ├── cps-ihm-client/         #     IHM polling client (background traffic)
│       └── cps-mgmt-console/       #     Console VM setup (FastAPI + frontend + SSH keys)
│
├── console/                        # Management console (reference / development)
│   ├── backend/
│   │   ├── app.py                  #   FastAPI + asyncssh backend
│   │   └── requirements.txt       #   Python dependencies
│   └── frontend/
│       └── index.html              #   ICS Detection Console (attack + defense UI)
│
├── ludus/keys/                     # Disposable lab SSH key (committed on purpose)
│
└── docs/                           # Documentation
    ├── setup.md                    #   Install Ludus + template (before deploy)
    ├── deployment.md               #   Step by step deploy & test guide
    └── architecture.md             #   Full architecture design document
```

The `console/` directory holds the reference sources for development. The same files are embedded in `ludus/roles/cps-mgmt-console/files/` for Ansible deployment. Edit in `console/`, then sync to the role.

## Prerequisites

A host running [Ludus](https://docs.ludus.cloud) with the `debian-12-x64-server-template` built. If you do not have Ludus yet, `docs/setup.md` walks through installing it and building the template from scratch (you do not install Suricata, OIDA, or the ICS libraries yourself, the roles do that at deploy time).

The range itself needs about 10 GB RAM minimum (16+ GB comfortable) and roughly 92 GB disk.

## Deployment

```bash
# 1. Clone the repo
git clone https://github.com/<your-org>/cps-lab.git
cd cps-lab

# 2. Add the Ansible roles to Ludus
for role in ludus/roles/cps-*; do
  ludus ansible role add -d "$role"
done

# 3. Deploy the range
ludus range config set -f ludus/range-config.yml
ludus range deploy

# 4. Wait for deployment (~15-20 min)
ludus range status

# 5. Access the console
# Open http://<console-ip>:8080 in your browser
# (Console IP = 10.x.99.10 where x is your Ludus range ID)
```

## Console Interface

The web console at `http://console:8080` provides two panels.

**Red Team (left)** — Attack execution following the kill chain. Each phase header groups OIDA commands that run on the appropriate pivot host via SSH. Output appears in a dedicated terminal pane.

**Blue Team (right)** — Suricata rule writing and log monitoring. Rules are pushed to probe S1 (L3/L2 traffic on eth0.30) or S2 (L2/L1 traffic on eth0.10) on the NGFW, with real-time fast.log streaming via WebSocket.

### Connected vs Demo Mode

The interface auto-detects backend availability.

In **Connected mode**, all commands execute live via SSH on the actual VMs.

In **Demo mode** (backend unreachable), the interface uses hardcoded OIDA outputs and simulated detection. Useful for presentations and dry runs without a live lab.

Click the mode badge (top-right) to toggle manually.

## Network Segmentation

| VLAN | Layer | Subnet | Purpose |
|------|-------|--------|---------|
| 99 | Management | 10.x.99.0/24 | Console (SSH orchestration) |
| 30 | L3 Supervision | 10.x.30.0/24 | Historian, EWS |
| 20 | L2 Control | 10.x.20.0/24 | OPC UA srv, IHM |
| 10 | L1 Field | 10.x.10.0/24 | PLC (Modbus, S7, OPC UA) |

Inter-VLAN default policy: **REJECT**. Only the traffic paths required by the kill chain and normal operations are allowed (see `ludus/range-config.yml` for the full firewall matrix).

## ICS Protocols Simulated

| Protocol | Port | Simulator | VM |
|----------|------|-----------|----|
| S7comm | 102 | snap7 (CPU 315-2 PN/DP) | PLC |
| Modbus TCP | 502 | pymodbus (16 coils, 100 HR) | PLC |
| OPC UA | 4840 | python-opcua (Shopfloor + Field) | OPC UA srv, PLC |
| PostgreSQL | 5432 | PostgreSQL 15 (SUPERUSER) | Historian |

## Suricata Probes

Both probes run on the NGFW (Ludus router) using af-packet on VLAN sub-interfaces.

| Probe | Interface | Covers |
|-------|-----------|--------|
| S1 | eth0.30 | L3 to/from L2 (recon, SSH pivot) |
| S2 | eth0.10 | L2 to/from L1 (exploitation) |

Custom rules are written to `/etc/suricata/rules/custom_s1.rules` or `custom_s2.rules` and hot-reloaded via `suricatasc`.

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/oida/execute` | Run OIDA command on historian or opcua_srv |
| POST | `/api/suricata/rules` | Push rules to probe S1 or S2 |
| GET | `/api/suricata/rules/{probe}` | Read current custom rules |
| GET | `/api/status` | SSH connectivity check (all targets) |
| WS | `/ws/logs/{probe}` | Real-time Suricata fast.log stream |
| GET | `/health` | Backend health check |

## Contributing

Contributions welcome. The main areas for improvement:

- Additional OIDA attack scenarios
- More Suricata rule templates for ICS protocols
- Windows-based VMs (HMI with WinCC, engineering workstation)
- Grafana dashboard for Suricata stats
- Score tracking for workshop gamification

## License

MIT

## Acknowledgements

- [Ludus](https://docs.ludus.cloud) — Cyber range automation
- [OIDA](https://getoida.dev) — OT/ICS Dynamic Assessment Framework
- [Suricata](https://suricata.io) — Network IDS/IPS
- [snap7](https://snap7.sourceforge.net) — S7comm communication library
- [pymodbus](https://github.com/pymodbus-dev/pymodbus) — Modbus protocol library
- [python-opcua](https://github.com/FreeOpcUa/opcua-asyncio) — OPC UA implementation
