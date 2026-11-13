# Lab SSH key (DISPOSABLE)

This directory documents the single keypair the console uses to reach the
target VMs over SSH. The authoritative copies live in the role `files/`:

- private key: `roles/cps-mgmt-console/files/cps_console_ed25519` (deployed to the console)
- public key:  `roles/cps-oida-ready/files/` and `roles/cps-ngfw-suricata/files/` (authorized on targets)

**This keypair is throwaway, lab-only, and committed on purpose** so the range
deploys in one pass with no fact-passing or VM-ordering tricks. It is consistent
with the lab's other intentional weaknesses (weak passwords, SUPERUSER Postgres).

NEVER reuse this key anywhere real. To rotate it, regenerate an ed25519 pair and
replace all four files above with the new private/public halves.
