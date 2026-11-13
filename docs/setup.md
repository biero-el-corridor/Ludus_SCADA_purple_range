# Setup Guide (before you deploy)

This covers everything that has to exist **before** `docs/deployment.md` picks up: what you install by hand, and what gets installed for you.

## 1. What you install by hand vs what is automatic

The important thing to understand first: you do **not** install Suricata, snap7, the OPC UA libraries, OIDA, or Python on each machine yourself. All of that is installed automatically by the Ansible roles during `ludus range deploy`, inside each VM, in isolated Python virtualenvs. (Section 6 explains exactly what gets installed where.)

By hand, you install only three things, once:

1. A host machine running Debian (or Proxmox).
2. Ludus on that host.
3. One VM template (`debian-12-x64-server-template`) that all 7 VMs are cloned from.

After that, the whole lab is reproducible with the three deploy commands.

## 2. Host requirements

Ludus runs on bare metal or a VM that itself can run VMs (nested virtualization). From the official requirements:

1. An x86_64 CPU with a Passmark score above 6000, with virtualization enabled (`vmx` for Intel or `svm` for AMD visible in `/proc/cpuinfo`). Nested virtualization works but is slower.
2. Debian 12/13, or an existing Proxmox 8/9 (Proxmox has its own guide).
3. At least 32 GB RAM and 200 GB disk (NVMe recommended). This comfortably covers the CPS lab's own budget (about 10 to 18 GB RAM, 92 GB disk).
4. Root access and a wired internet connection (WiFi is not supported).
5. Docker must NOT be installed on the host, because it breaks the networking Ludus sets up.

If you use a fresh Debian 13 netinst ISO, at the software selection screen uncheck "Debian desktop environment" and check "SSH server".

## 3. Install Ludus

SSH into the host, become root, install prerequisites, then run the one line installer:

```bash
ssh user@your-host
su -                                   # become root
apt update && apt install -y curl sudo ca-certificates

# All-in-one installer (installs the ludus client + prompts for the server)
curl --proto '=https' --tlsv1.2 -sSf https://ludus.cloud/install | bash
```

If you prefer to read it first:

```bash
curl https://ludus.cloud/install > install.sh
cat install.sh
chmod +x install.sh
./install.sh
```

The installer runs a first phase and reboots the machine. After the reboot it continues automatically. Watch its progress by SSHing back in, becoming root, and running:

```bash
ludus-install-status
```

Wait until it reports the install is complete before going further.

Reference: [Install Ludus](https://docs.ludus.cloud/docs/quick-start/install-ludus/).

## 4. Create your user and configure the client

Ludus has a server (on the host) and a `ludus` client (the CLI you drive it with). After the server install you create a user, which gives you an API key, and you point the client at the server with that key.

Follow the official steps here, as the exact commands depend on your Ludus version: [Create users](https://docs.ludus.cloud/docs/quick-start) and the API key setup in the same Quick Start section. The shape is:

1. On the host, add yourself a user (admin for the first one), then print the API key it generated:
   ```bash
   ludus users add -i <USERID> -n "<First Last>" -e <mail> -a
   ludus users apikey -u <USERID> --value --no-prompt
   ```
   Copy that key somewhere safe.

2. On the machine where you run the client, set the **server URL first**, before storing the key. The client's compiled-in default is `https://198.51.100.1:8080` (a documentation address), so a client that was never configured looks for a key for the wrong host and fails with:
   ```
   [FATAL] No Ludus API key found in system keyring for https://198.51.100.1:8080.
   ```
   Point it at your server, either persistently:
   ```bash
   mkdir -p ~/.config/ludus
   printf 'url: https://<host>:8080\n' > ~/.config/ludus/config.yml
   ```
   or per command with `ludus --url https://<host>:8080 ...`. Client 2.3.x has no `LUDUS_URL` environment variable: the URL comes from that file or the flag, nothing else.

3. Store the API key for that URL, or bypass the keyring on a headless box:
   ```bash
   ludus apikey                    # prompts for the key, stores it in the system keyring
   export LUDUS_API_KEY="<key>"    # alternative: used directly, no keyring involved
   ```

4. Confirm it works:
   ```bash
   ludus range list
   ```
   A clean (even empty) response means the client and server are talking.

You can run the `ludus` client directly on the host for simplicity, which avoids any URL or networking setup.

## 5. Build the VM template

Every VM in the range is cloned from a template. Build the Debian 12 one the range config expects, then confirm it is ready:

```bash
ludus templates build -n debian-12-x64-server-template
ludus templates list        # wait until it shows as built
```

Template builds take a while (they boot a VM, run an unattended install, and snapshot it). Let it finish before deploying.

## 6. What the roles install for you (and how the simulators work)

This answers a common question: the ICS servers are not off-the-shelf daemons, they are small mock programs shipped **inside the roles** as Jinja2 templates (the `.py.j2` files). At deploy time each role renders its template to a real Python file on the VM, installs the libraries it needs in a local venv, and runs it as a systemd service. So the protocol logic is "in the script directly", and the library underneath (snap7, opcua, pymodbus) comes from pip plus, for S7, a native system library.

| Service | VM | Role | Rendered to | Library | How it runs |
|---------|----|------|-------------|---------|-------------|
| OPC UA server (Shopfloor) | OPC UA srv | cps-opcua-server | `/opt/opcua-server/server.py` | `opcua` (pip) | systemd `opcua-server` |
| OPC UA field server | PLC | cps-opcua-field | `/opt/opcua-field/server.py` | `opcua` (pip) | systemd `opcua-field` |
| S7comm server | PLC | cps-s7comm | `/opt/s7comm-server/server.py` | `python-snap7` (pip) + `libsnap7` (apt) | systemd `s7comm-server` |
| Modbus server | PLC | cps-modbus | `/opt/modbus-server/server.py` | `pymodbus` (pip) | systemd `modbus-server` |
| IHM polling client | IHM | cps-ihm-client | `/opt/ihm-client/poll.py` | `opcua` + `python-snap7` (pip) | systemd `ihm-client` |
| OIDA (attacker tooling) | Historian, OPC UA srv | cps-oida-ready | installed as a package | `oida-ics[all]` (pip) | run on demand via the console |

### The OPC UA server

`roles/cps-opcua-server/templates/opcua_server.py.j2` is a complete server built on the `opcua` library. It creates a `Shopfloor` object with `Line_A` (Temperature, Pressure, Motor_RPM) and `Line_B` (Temperature, ValveState), marks the variables writable, exposes three security policies (None, Sign, SignAndEncrypt) with anonymous access allowed (intentionally weak), and runs a background thread that nudges the values every 2 seconds so the data looks live. It listens on `0.0.0.0:4840`. Nothing external, the whole server is those 70 lines.

### The S7comm server and the snap7 client

Two different uses of snap7, which is the usual confusion point:

The **server** side (`roles/cps-s7comm/templates/s7_server.py.j2`) uses `snap7.server.Server()` to speak the S7comm protocol natively on port 102. It registers data blocks (DB1 process data, DB2 setpoints, DB10 alarms, DB100 config) as raw byte arrays, writes initial float/int values with `struct.pack_into`, and drifts the temperature every 2 seconds. This is what a real PLC exposes.

The **client** side appears in two places. The IHM (`roles/cps-ihm-client/templates/ihm_poll.py.j2`) uses `snap7.client.Client()` to connect to the PLC and read DB1 every 10 seconds, which produces the normal background traffic the blue team sees. The attacker's client is OIDA, installed by `cps-oida-ready`, which is what the red team drives from the console to identify and manipulate the same PLC.

Both the server and the client need the native `libsnap7` shared library in addition to the `python-snap7` pip wrapper. The roles install it via apt (`libsnap7-1`) best effort; if your Debian repo does not carry it, enable the build-from-source block in `cps-s7comm` (flip its `when:` to `true`) so it compiles snap7 from GitHub.

## 7. Next

Once Ludus is installed, your user and client are configured, and the template is built, continue with `docs/deployment.md` from step 2 (get the repo) onward.

---

Sources: [Install Ludus](https://docs.ludus.cloud/docs/quick-start/install-ludus/), [Ludus Quick Start](https://docs.ludus.cloud/docs/quick-start), [Ludus on Proxmox](https://docs.ludus.cloud/docs/deployment-options/proxmox).
