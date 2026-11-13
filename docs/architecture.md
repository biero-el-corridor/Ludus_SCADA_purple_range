# CPS Lab — Architecture & Connectivity Design

**v1.0**
Based on: spec_lab_cps_v4.pdf

---

## 1. Overview

A 7-VM ICS/OT cyber range deployed via Ludus/Ansible, simulating a 3-layer Purdue-model industrial network. A central NGFW (the Ludus router) handles inter-VLAN routing, firewalling, and Suricata IDS. A management console on VLAN 99 orchestrates both the attack side (OIDA commands) and the defense side (Suricata rules).

### Key Design Decision

The Ludus router acts as the NGFW — there are no dedicated probe VMs. Suricata runs directly on the router's VLAN sub-interfaces (eth0.30 for L3/L2 traffic, eth0.10 for L2/L1 traffic). The global interface maintains the Purdue segmentation view for pedagogical clarity even though the underlying infra uses a flat router topology.

---

## 2. Network Architecture

### VLANs & Addressing

| Layer | VLAN | Subnet | Hosts |
|-------|------|--------|-------|
| L3 Supervision | 30 | 10.x.30.0/24 | Historian .10, EWS .11 |
| L2 Control | 20 | 10.x.20.0/24 | OPC UA srv .10, IHM .11 |
| L1 Field | 10 | 10.x.10.0/24 | PLC .11 (Modbus+S7+OPC UA) |
| Management | 99 | 10.x.99.0/24 | Console .10 |

Gateway for all VMs = NGFW at .254 on each VLAN.

### Firewall Matrix (NGFW network.rules)

| Source | Destination | Ports | Action |
|--------|-------------|-------|--------|
| L3 (30) | OPC UA srv (20) | tcp 22, 4840 | ACCEPT |
| L2 (20) | PLC (10) | tcp 22, 102, 502, 4840 | ACCEPT |
| Console (99) | Historian (30), OPC UA srv (20) | tcp 22 | ACCEPT (mgmt) |
| L3 (30) | L1 (10) | all | REJECT |
| any other inter-VLAN | — | all | REJECT |

Note: Ludus filters VLAN-to-VLAN, not per host. The single L2 to L1 rule covers both the OPC UA srv and the IHM (both on VLAN 20). Restricting the IHM alone to S7comm is not achievable at the firewall and exists only as a nuance in the console interface.

### Suricata Probes on NGFW

| Probe | Interface | Coverage | Rationale |
|-------|-----------|----------|-----------|
| S1 | eth0.30 | L3/L2 | L3 only communicates with L2 |
| S2 | eth0.10 | L2/L1 | L1 only communicates with L2 |

All inter-VLAN traffic transits the NGFW, so the kill chain flows (SSH L3 to L2, OPC/Modbus/S7 L2 to L1) are visible. Intra-VLAN traffic (e.g. IHM to OPC UA srv on VLAN 20) does NOT traverse the NGFW.

---

## 3. Kill Chain

The attack scenario follows a staged progression:

1. **Initial compromise** (presented orally): Historian already compromised via pg_execute_server_program
2. **Recon from Historian** (L3 to L2): OIDA commands executed on Historian targeting OPC UA srv
   - `oida opcua discover 10.x.20.10` — endpoint discovery
   - `oida opcua browse 10.x.20.10` — node enumeration
3. **Lateral movement** (L3 to L2): SSH pivot from Historian to OPC UA srv
   - `ssh lowpriv@10.x.20.10` — using credentials found during recon
4. **Recon from OPC UA srv** (L2 to L1): OIDA commands targeting PLC
   - `oida s7 -i 10.x.10.11` — S7comm device identification
   - `oida s7 list-blocks 10.x.10.11` — program block enumeration
5. **Exploitation** (L2 to L1): OIDA attack commands on PLC
   - `oida s7 stop 10.x.10.11` — CPU STOP
   - `oida s7 write 10.x.10.11 --area DB --db 1 --offset 0 --type REAL --value 999.9`
   - `oida modbus write-coil 10.x.10.11 --addr 0 --count 8 --value 0xFF`

---

## 4. Connectivity Solution — Console Backend

### Architecture

```
Console VM (VLAN 99 — 10.x.99.10)
│
├── FastAPI backend (:8080)
│   │
│   ├── POST /api/oida/execute
│   │   ├── target: "historian" → SSH 10.x.30.10 (port 22)
│   │   │   └── runs: oida opcua discover/browse on L2 targets
│   │   └── target: "opcua_srv" → SSH 10.x.20.10 (port 22)
│   │       └── runs: oida s7/modbus commands on L1 targets
│   │
│   ├── POST /api/suricata/rules
│   │   └── SSH to NGFW (10.x.30.254) → write to /etc/suricata/rules/custom.rules
│   │       then: suricatasc -c reload-rules
│   │
│   ├── WebSocket /ws/logs/{probe}
│   │   └── SSH to NGFW → tail -f /var/log/suricata/fast-{s1,s2}.log
│   │
│   └── GET /api/status
│       └── checks SSH connectivity to all targets
│
└── Frontend (served at :8080/)
    └── index.html
```

### SSH Key Distribution (Ansible)

The lab uses a single pre-generated, disposable ed25519 keypair shipped with the roles (`ludus/keys/` documents it; the authoritative copies live in each role's `files/`). At deploy time:
- `cps-mgmt-console` copies the private key to the console (`~/.ssh/id_ed25519`).
- `cps-oida-ready` authorizes the public key for user `oida-runner` on Historian and OPC UA srv.
- `cps-ngfw-suricata` authorizes the public key for user `suricata-mgmt` on the NGFW.

Each role reads the key from its own `files/` with `lookup('file', ...)`, so the range deploys in one pass with no per-host fact passing and no dependence on VM provisioning order. The keypair is throwaway and committed on purpose (consistent with the lab's other intentional weaknesses); it must never be reused anywhere real.

### Dual Mode: Connected vs Demo

The interface supports two modes:
- **Connected mode** (default when backend is reachable): real SSH execution, live logs
- **Demo mode** (fallback): hardcoded OIDA outputs and simulated detection, for presentations without the lab

The frontend checks `GET /api/status` on load. If unreachable, it falls back to demo mode with a banner.

---

## 5. VMs Summary

| # | VM | VLAN | IP | Services | Kill Chain Role |
|---|-----|------|----|----------|-----------------|
| 1 | Historian | 30 | .10 | PostgreSQL 5432, SSH 22 | Target (1)(2), executes OIDA |
| 2 | EWS | 30 | .11 | admin | Authorized L3 host |
| 3 | OPC UA srv | 20 | .10 | OPC UA 4840, SSH 22 | Pivot (3), executes OIDA |
| 4 | IHM | 20 | .11 | OPC UA client + S7comm | L1/L2 consumer |
| 5 | PLC | 10 | .11 | Modbus 502, S7 102, OPC UA 4840 | Final target |
| 6 | NGFW | central | .254 | routing + fw + Suricata | Segmentation + detection |
| 7 | Console | 99 | .10 | FastAPI + UI | Management plane |

---

## 6. Resource Budget

| VM | Comfortable | Minimal |
|----|-------------|---------|
| Historian | 2 vCPU / 4 GB / 20 GB | 2 / 2 GB / 20 GB |
| EWS | 2 / 2 GB / 12 GB | 1 / 1 GB / 12 GB |
| OPC UA srv | 2 / 2 GB / 12 GB | 1 / 1 GB / 12 GB |
| IHM | 2 / 2 GB / 12 GB | 1 / 1 GB / 12 GB |
| PLC | 2 / 2 GB / 12 GB | 1 / 1 GB / 12 GB |
| NGFW | 4 / 4 GB / 12 GB | 2 / 3 GB / 12 GB |
| Console | 2 / 2 GB / 12 GB | 1 / 1 GB / 12 GB |
| **Total** | **16 vCPU / 18 GB / 92 GB** | **9 / 10 GB / 92 GB** |

Host requirement: ~24 GB comfortable / 16 GB minimum.

---

## 7. Ansible Roles Reference

| Role | Target VM | Purpose |
|------|-----------|---------|
| cps-ngfw-suricata | NGFW (router) | Suricata IDS, 2 instances (S1/S2), af-packet, ET Open + ICS rules, reload helper, suricata-mgmt user |
| cps-postgres-vuln | Historian | PostgreSQL 15 with SUPERUSER app user, sample process_data table |
| cps-oida-ready | Historian, OPC UA srv | oida-runner user, SSH key, Python venv (snap7/opcua/pymodbus), OIDA wrapper |
| cps-opcua-server | OPC UA srv | Mock OPC UA (Shopfloor namespace, Line_A/Line_B, 3 security policies) |
| cps-opcua-field | PLC | OPC UA field-level (raw I/O: AI0-2, DO0-2) |
| cps-s7comm | PLC | Mock S7comm (snap7, CPU 315-2 PN/DP, DB1/DB2/DB10/DB100) |
| cps-modbus | PLC | Mock Modbus TCP (pymodbus, 16 coils, 100 holding registers) |
| cps-ssh-lowpriv | OPC UA srv | Weak SSH user for lateral movement scenario |
| cps-ihm-client | IHM | Polling client (OPC UA L2 + S7comm L1), background traffic every 10s |
| cps-mgmt-console | Console | SSH keypair gen, FastAPI + frontend deploy, targets.yml, systemd unit |
