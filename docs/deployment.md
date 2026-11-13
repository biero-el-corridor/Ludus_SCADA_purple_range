# Deployment & Test Guide

A step by step walkthrough to deploy the CPS lab on Ludus and verify it works, written for someone deploying their first Ludus range. Every command is meant to be run from the machine where your `ludus` client is configured.

## 1. Prerequisites

Before anything, confirm the ground is ready.

1. A Ludus server is installed and reachable, and `ludus` on your machine is configured against it (an API key is set). Check with:
   ```bash
   ludus range list
   ```
   If that returns without an auth error, you are good. `[FATAL] No Ludus API key found in system keyring for https://198.51.100.1:8080` does not mean the key is missing: it means the client URL was never set and it is looking for a key for its built-in placeholder address. See `docs/setup.md` step 4.
2. The Proxmox host behind Ludus has the `debian-12-x64-server-template` built. Check with:
   ```bash
   ludus templates list
   ```
   If it is missing, build it first (`ludus templates build -n debian-12-x64-server-template`) and wait for it to finish.
3. The host has room for the range. The budget is about 10 GB RAM and 9 vCPU minimum (16 GB / 16 vCPU comfortable), plus roughly 92 GB disk.

## 2. Get the repo onto the Ludus client

Clone the repository where your `ludus` client runs (the roles are read locally and pushed to the server).

```bash
git clone https://github.com/<your-org>/cps-lab.git
cd cps-lab
```

Everything below is run from this directory.

## 3. Add the Ansible roles to Ludus

Ludus keeps its own library of roles. You register each role once, and from then on you can reference it by name in any range config. Add all ten:

```bash
for role in ludus/roles/cps-*; do
  echo ">>> adding $role"
  ludus ansible roles add -d "$role"
done
```

Verify they are registered:

```bash
ludus ansible roles list
```

You should see `cps-ngfw-suricata`, `cps-postgres-vuln`, `cps-oida-ready`, `cps-opcua-server`, `cps-opcua-field`, `cps-s7comm`, `cps-modbus`, `cps-ssh-lowpriv`, `cps-ihm-client`, `cps-mgmt-console`.

(If your Ludus version uses the singular form, the command is `ludus ansible role add -d "$role"`. Both exist across versions.)

## 4. Load the range config

Point your range at `range-config.yml`:

```bash
ludus range config set -f ludus/range-config.yml
```

Then look at what Ludus understood, especially the second octet it assigned to your range (it replaces the `10.x` in the docs with a real number, for example `10.2`):

```bash
ludus range config get
```

Note that number. Your console will live at `10.<that>.99.10`, the PLC at `10.<that>.10.11`, and so on.

## 5. Deploy

```bash
ludus range deploy
```

This builds the 7 VMs, then runs the roles on each. Expect fifteen to twenty-five minutes. The slowest single step is the console and OIDA Python installs (the `oida-ics[all]` package pulls a lot).

Watch it live in a second terminal:

```bash
ludus range logs -f
```

If a role fails, Ludus stops and shows you the failing task by name. Fix the role, then just rerun `ludus range deploy` (the roles are idempotent, so completed steps are skipped).

## 6. Check the range is up

```bash
ludus range status
```

All 7 VMs plus the router should show as deployed. Now spot check a few services. The easiest way in is the console VM, which already holds the SSH key to everything.

```bash
# SSH into the console (via the Ludus-provided access, e.g. through the router/VPN)
ssh console@10.<octet>.99.10     # or use `ludus range ssh` if your setup provides it

# from the console, the SSH shortcuts are pre-written:
ssh historian 'systemctl is-active postgresql'
ssh opcua_srv 'systemctl is-active opcua-server'
ssh ngfw      'systemctl is-active suricata-s1 suricata-s2'
```

On the PLC, confirm the three protocol servers listen:

```bash
ssh opcua_srv            # pivot is reachable from console
# then, as a quick port check from the OPC UA srv toward the PLC:
nc -z -v 10.<octet>.10.11 102   # S7comm
nc -z -v 10.<octet>.10.11 502   # Modbus
nc -z -v 10.<octet>.10.11 4840  # OPC UA
```

## 7. Open the console interface

In a browser that can reach the management VLAN, go to:

```
http://10.<octet>.99.10:8080
```

The interface checks the backend on load. If the backend answers (`/api/status`), the mode badge at the top right shows **CONNECTED** and every action runs for real over SSH. If it cannot reach the backend, it shows **DEMO** and uses canned output, which is what you want for a presentation without the lab. You can click the badge to switch manually.

Confirm connectivity from the backend side:

```bash
curl http://10.<octet>.99.10:8080/api/status
# expect: {"historian":true,"opcua_srv":true,"ngfw":true}
```

If any of those three is `false`, see troubleshooting below.

## 8. Run the kill chain to test detection

This is the actual workshop loop, and the best end to end test.

1. In the **Blue Team** panel, pick probe **S2** (it watches L2 to L1, where exploitation happens) and write a simple rule, for example an alert on S7comm traffic to the PLC:
   ```
   alert tcp any any -> 10.<octet>.10.11 102 (msg:"S7comm to PLC"; sid:1000010; rev:1;)
   ```
   Push it. The backend writes `custom_s2.rules` on the NGFW and hot reloads probe S2.
2. In the **Red Team** panel, run a phase 4 recon action against the PLC (an `oida s7` identification). It executes on the OPC UA srv over SSH and prints OIDA output in the terminal pane.
3. Back in Blue Team, the live log stream (WebSocket tail of `fast.log`) should show your alert firing. That round trip (rule pushed, attack run, alert seen) is the proof the whole chain works.

If you want to confirm by hand, on the NGFW:

```bash
ssh ngfw 'sudo tail -n 20 /var/log/suricata/s2/fast.log'
```

## 9. Troubleshooting

**`/api/status` shows a target as false.** The console could not SSH there. Check that the target authorized the lab key:
```bash
ssh ngfw 'cat ~suricata-mgmt/.ssh/authorized_keys'
ssh historian 'cat ~oida-runner/.ssh/authorized_keys'
```
They should contain the `cps-console-lab-key (DISPOSABLE)` public key. If empty, the `cps-oida-ready` / `cps-ngfw-suricata` role did not run or failed, rerun `ludus range deploy`.

**Rules push succeeds but no alert ever fires.** Confirm the probe actually loaded your file and the socket exists:
```bash
ssh ngfw 'ls -l /run/suricata-s2.socket; sudo suricatasc -c reload-rules /run/suricata-s2.socket'
ssh ngfw 'cat /etc/suricata/rules/custom_s2.rules'
```
Also remember S1 only sees L3 to L2 traffic and S2 only L2 to L1, so a rule on the wrong probe will never match.

**An OIDA command errors with a module or library error.** The native snap7 library may not have installed from apt on that VM (it is best effort). Check:
```bash
ssh opcua_srv 'ldconfig -p | grep snap7'
```
If absent, enable the build-from-source block in `cps-s7comm` (set its `when:` to `true`) or install `libsnap7` by hand, then redeploy. OIDA itself lives at `/opt/oida/venv/bin/oida`; test it directly:
```bash
ssh opcua_srv 'oida --help'
```

**OIDA subcommand syntax.** The attack strings in the interface follow the documented `oida <protocol> <target>` pattern, but confirm the exact tokens for your OIDA version against the protocol pages (getoida.dev). If a token differs, adjust it in the frontend attack definitions, the rest of the plumbing is unaffected.

**A role fails on `restart sshd` or a handler.** Make sure you deployed from this corrected repo (the `cps-ssh-lowpriv` role now ships a handler and no longer sets `AllowUsers`). An older copy will break here.

## 10. Teardown

To wipe the range (VMs destroyed, roles stay registered for next time):

```bash
ludus range rm
```

To redeploy clean afterward, repeat from step 5.
