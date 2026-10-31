# Runbook: a remote run

A remote run creates billable VMs and moves funds. Every step that does so
asks before acting. Do not pass `--yes` unless you have decided in advance.

## 0. One-time setup

Create `~/.config/gvpn-bench/config.toml` (mode 600). Every key is
optional; the values shown are the defaults:

```toml
runs_dir = "~/.local/state/gvpn-bench/runs"
identity_backup_dir = "~/.local/state/gvpn-bench/identities"

[credentials]
digitalocean_token_file = "~/do-api-token"   # contains a dop_v1_ token
funding_key_file = "~/key.txt"               # private_key:, eoa:, safe: lines

[results]
repo = "~/gnosis_vpn-results"                # local clone of the PRIVATE results repo
```

Check with `./gnosis_vpn-bench config`. It warns about credential files
that group or others can read; fix them with `chmod 600`. The tool never
moves them.

For `fund` and `recover`, install the extra: `pip install -e '.[remote]'`.

## 1. Plan

```bash
./gnosis_vpn-bench plan --profile v5-piz-palu-dev --target kaunas
```

This creates `<runs_dir>/<id>/run.toml` and refuses a profile and target
on different networks. Edit `run.toml` to change VM sizes, the region, the
number of clients, the deletion deadline or the ladder. Defaults: one
`c-2` origin plus three `c-4` clients in `fra1`; a 4 h deadline;
200,000,000-byte transfers, a 300 s deadline, 10 s gaps and rungs 1, 2, 3.

## The usual way: `execute`

```bash
./gnosis_vpn-bench plan --profile v4-jura-prod --target netherlands-jura-prod --target austria-jura-prod
./gnosis_vpn-bench execute RUN
```

`execute` asks once for the whole run and once more, after reading each
client's request, before sending funds (`--yes` answers both). Declining the funding prompt
ends the run and tears it down, deleting the droplets just provisioned. It then
runs steps 2–7 below: the same funded clients connect to and ladder each
target in turn. Results are pushed to the private results repo without
asking (step 8): after each target, and once more with all of the run's
results before teardown. A refused scan or a failed push is reported
loudly (`RESULTS NOT PUSHED`) but never blocks teardown or the deletion
timer; retry with `results push RUN`. Teardown (recover, then
destroy) always runs at the end, including after an error or Ctrl-C. If
teardown itself fails, it says so; the deletion timer is still armed, and
`recover RUN` / `destroy RUN` can be rerun by hand. `--keep` skips
teardown; the timer still deletes the droplets at the deadline.

Results land in `RUN/results/<target>/` (result.json, report.html,
provenance.json); `report RUN` and `results push RUN` handle every target.

## 2. Provision

```bash
./gnosis_vpn-bench provision RUN
```

1. The deletion timer is scheduled first (`systemctl --user list-timers`).
   When it fires, it deletes the run's droplets. On a funded, unrecovered
   run it deletes only if every funded client's key files (`.id`, `.pass`)
   are backed up, so funds stay recoverable from the backups. The Safe file
   is not required for this check: it only exists once a client has
   deployed its Safe, and the Safe address is also kept in `safe-info.json`
   after connecting.
2. The droplets are created, tagged `gvpn-bench-run-<id>`, one at a time;
   the inventory is saved after each.
3. The origin serves one random file of `transfer_bytes` via nginx.
4. Each client installs the profile's pinned package (sha256 checked) and
   starts its worker (v5: as a dedicated non-root user).

## 3. Fund

```bash
./gnosis_vpn-bench fund RUN
```

The step pins the network's chain metadata. It reads each client's funding
recommendation and backs up its key files to
`<identity_backup_dir>/<network>/<run>/<client>/`, then shows the amounts
and asks before sending them from the funding Safe.

Every transaction is journaled in `state/transactions.jsonl` before it is
broadcast. If a transaction has no receipt (`AmbiguousTransactionError`),
nothing more is sent. Look it up on a block explorer, then rerun: a mined
transaction is reconciled from its receipt. One that never reached the
network is marked dropped, and the next transaction from that sender is
forced onto the same nonce, so only one of the two can execute (the other
is then marked superseded). If that nonce was consumed by a transaction
the journal does not know, sending stops. Funding never pays a node twice.

## 4. Connect

```bash
./gnosis_vpn-bench connect RUN [--target NAME]   # default: the run's first target
```

The step starts `ssh-keeper` and `deadman` on each client and a local
heartbeat process (`state/heartbeat.pid`). It runs a direct (non-VPN)
download from the origin as a baseline, then connects every client to the
target **by address**. A client counts as connected only when the egress
IP changes and the routes to the origin and the ping target go through the
tunnel. Otherwise its failure gets a status from the fixed list. Clients
first disconnect from any previous target, so a run can test several exits.

`connect` then backs up each client's identity again, now including the
Safe file; for a connected client a missing Safe file is an error, and a
changed key file is always refused.

If the operator machine goes away, `deadman` stops the VPN after 6 min and
reboots the VM once after 12 min.

## 5. Measure

```bash
./gnosis_vpn-bench ladder RUN [--target NAME]   # 1 → 2 → 3 concurrent clients
./gnosis_vpn-bench sweep RUN        # every discovered exit once
./gnosis_vpn-bench --run RUN --on client1 ramp   # any local mode on a VM
```

`ladder` and `sweep` write `result.json`, `report.html` and
`provenance.json` to `RUN/results/<target>/` (a sweep reports under the
run's first target), then push them to the private results repo without
asking (`--no-push` to skip). A rung whose clients are not all still
connected is `failed`; the rungs after it are `not_run`.

## 6. Recover

```bash
./gnosis_vpn-bench recover RUN
```

1. The clients are halted: balance snapshot; worker disconnected, stopped
   and masked; guards stopped; v5 payment state backed up. Then the local
   heartbeat stops.
2. Closure starts on every outgoing channel, taken from the client's list
   and the on-chain `ChannelOpened` logs. The journal is saved as
   `state/recovery.json`.
3. After the closure grace period, the closures are finalized and the
   wxHOPR and xDAI are returned to the funding Safe. A small gas reserve
   stays on each node; its key is backed up. The returns are audited from
   receipts into `state/cleanup.json`.

An interrupted recovery can be rerun. If the journal is incomplete, steps 1
and 2 run again (halting is safe to repeat); otherwise only step 3 runs.

## 7. Destroy

```bash
./gnosis_vpn-bench destroy RUN
```

The step re-verifies against the chain that all channels are closed and
that the balances match `cleanup.json`. Then it deletes only droplets
listed in this run's inventory **and** tagged for this run, confirms they
are gone, and cancels the deletion timer.

## 8. Publish

```bash
./gnosis_vpn-bench results push RUN --dry-run
./gnosis_vpn-bench results push RUN [RUN ...] [--title T] [--link "Claude app=https://claude.ai/artifact/…"]
```

The private results repository is laid out as:

```
README.md                  index of every report and recording
runs/<start>-<exit>-<profile>/      one recording (one run × one exit)
reports/<start>-<exits>-<profiles>/ one report over one or more recordings
```

Each folder has `README.md` and `charts/*.svg`, which GitHub renders on
the web and in its app, plus `report.html` (the same report as one page).
Recordings also keep `result.json` and `provenance.json`.

Results are pushed automatically by `ladder`, `sweep` and `execute` as
soon as they exist, before any machine is destroyed; run `results push`
by hand only to retry or to publish several runs together. A single
run's report is named after the run id, so later pushes update it and
keep links attached with `results link`.

`results push` adds the given runs' recordings and one report over all of
them: one line per exit in the overview chart, and error bars (min to max
around the mean) where the same exit, network, profile and client package
were recorded more than once. Other commands:

```bash
./gnosis_vpn-bench results combine REC1 REC2 ... [--title T]   # report over recordings already in the repo
./gnosis_vpn-bench results link REPORT "Claude app" https://claude.ai/artifact/…
./gnosis_vpn-bench results rebuild                             # regenerate every page after a renderer change
```

Every command scans all generated files and refuses private keys, tokens
and VM or other public IP addresses, writes only allow-listed file names,
and refuses public repositories. It does not ask before writing,
committing and pushing (`--ask` adds a prompt; `--dry-run` only scans). It never creates the results
repository; create it on GitHub as **private** yourself.

## Status at any time

```bash
./gnosis_vpn-bench status RUN
```

Shows the steps done, the droplets from the inventory, and where the funds
are (sent, recovered or still held by the clients).

## Historical results

The October 2026 ladder-repo outcomes can be converted with:

```bash
python3 -m gvpn_bench.results.historical ~/gnosis-vpn-serbia-ladder ~/gnosis-vpn-lithuania-ladder
```

They are marked `reproducible: false`; none reached ladder rung 1.
