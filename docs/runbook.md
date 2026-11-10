# Runbook: a remote run

A remote run creates billable VMs and moves funds. Every step that does so
asks before acting. Do not pass `--yes` unless you have decided in advance.
Publishing results is the exception: by the user's decision, results go to
the private results repo automatically, without a prompt (step 8).

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

`fund` and `recover` need web3, and so do `provision` and `execute` for a
profile with a `[funding]` estimate (their pre-flight reads the funding
Safe's balance). Install the extra into the checkout's `.venv`:
`python3 -m venv .venv && .venv/bin/pip install -e '.[remote]'`. The
launcher re-executes itself with `.venv/bin/python` when that exists
(`GVPN_BENCH_NO_VENV=1` opts out), and `config` fails with
`web3 : MISSING` when the interpreter in use cannot import web3.

## 1. Plan

For an exit not tested before, find the closest region first:

```bash
./gnosis_vpn-bench probe-regions --target kenya-jura-prod [--region ams3 --region fra1 ...]
```

It reads the exit's announced IPv4 address from the indexer, asks before
creating one `s-1vcpu-512mb-10gb` droplet per region (tag
`gvpn-bench-probe-<id>`), measures 10 pings and an `mtr` from each, then
deletes every droplet with that tag (also one whose create call failed)
and checks none is left, on every path including an error or Ctrl-C;
leftovers are logged as `PROBE DROPLETS NOT DELETED`. Exits that ignore
ICMP get the RTT of the last hop that answered, a lower bound for the
exit. It prints the regions fastest first; set `region` in `run.toml` to
the first one.

```bash
./gnosis_vpn-bench plan --profile v5-piz-palu-dev --target kaunas
```

This creates `<runs_dir>/<id>/run.toml` and refuses a profile and target
on different networks. Edit `run.toml` to change VM sizes, the region, the
number of clients, the deletion deadline or the ladder. Defaults: one
`c-2` origin plus three `c-4` clients in `fra1`; a 4 h deadline;
200,000,000-byte transfers, a 90 s deadline, 10 s gaps and rungs 1, 2, 3.
Runs planned before 2026-10-31 used a 300 s deadline; a slow exit then
spent the full 300 s per rung for the same overlap-based rate.

Before the first droplet, `provision` (and so `execute`) checks every
target on the network's indexer: the exit must be a registered node with
an announced address, and for a v5 profile that discovers exits it must
announce a `gvpn:exit` service. The profile's pinned package URL must
answer HTTP 200 (apt pools drop old releases; pin the GitHub release
asset instead). It also checks capacity: the DigitalOcean account must
have room for every droplet of the run (when the API token cannot read the
account, it says the limit is unknown and goes on), and when the profile
has a `[funding]` estimate, the funding Safe must hold `clients ×` that
much wxHOPR and xDAI. A failed check creates nothing. `fund` itself sends
nothing unless the Safe covers every outstanding transfer, so a run is
never left with only some clients funded.

Connecting gives up on a client early when the exit refuses its
registration twice (HTTP 404 on `clients/register`, i.e. no free slot),
or once when the exit already reports no free slot. The ladder then uses
only the connected clients: a rung needing more fails with that client's
status (usually `http_404_register`). A client that dropped before a rung
is reconnected once; the rung's `detail` says so, and its `gap_before_s`
shows the real gap.

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
changed key file is always refused. It also saves every client's Safe
info, including clients the exit refused, because recovery needs it.

`connect` exits 0 when at least one client connected, and says how many
of the clients did; the ladder then runs with the connected ones. It exits
1 only when no client connected.

If the operator machine goes away, `deadman` stops the VPN after 6 min and
reboots the VM once after 12 min.

## 5. Measure

```bash
./gnosis_vpn-bench ladder RUN [--target NAME] [--passes N]   # 1 → 2 → 3 concurrent clients
./gnosis_vpn-bench sweep RUN        # every discovered exit once
./gnosis_vpn-bench --run RUN --on client1 ramp   # any local mode on a VM
```

`ladder` and `sweep` write `result.json`, `report.html` and
`provenance.json` to `RUN/results/<target>/` (a sweep reports under the
run's first target), then push them to the private results repo without
asking (`--no-push` to skip). A rung whose clients are not all still
connected is `failed`; the rungs after it are `not_run`.

Before rung 1 every connected client pings the ping target 30 times
through its tunnel with no transfer running (`concurrent.idle_ping`).
`--passes N` runs the whole ladder N times; pass 2 onwards writes to
`RUN/results/<target>-pass<N>/`, raw data goes to one folder per pass
under `RUN/data/<target>/`, and the results are pushed after every pass.

The run log is `RUN/logs/run.log`: every step of the run appends its log
records (UTC timestamps) and everything it prints, line by line, so one
`tail -F` follows a whole run, `execute` included. It holds VM addresses
and is never published. Retries that are expected while a VM boots
(refused SSH, cloud-init not done, guard units not loaded yet) are logged
at DEBUG and do not appear in it. It has, per rung, `rung N: X of Y clients connected; exit slots
a/b in use` and then one line per client, `rung N <client>: <Mbit/s>
Mbit/s, <bytes> bytes, complete <bool>, ping <ms> ms`. Every step ends
with `step <name> finished: exit <code>`. A run's last line is `step
destroy finished` when the steps run one by one, and `step execute
finished` for `execute` (which recovers and destroys inside itself); a
log watcher can stop at either.

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

A failed `balance` call (a busy worker, ctl exit 70) is retried 3 times,
10 s apart, before recovery falls back to the saved Safe info.

After the returns, `recover` adds `payment` (funded, returned and spent
wxHOPR) to every result of the run, re-renders the reports and pushes them
again, before any droplet is deleted. On a PIX network (v5) that carried
traffic but spent nothing, it adds a `pix_no_payment` entry to
`warnings`, which the report shows above everything else.

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

A push prints `App copy: <path>`: the report's self-contained
`report.html` in the results checkout. Publish that file as it is (for
example to the Claude app); do not edit a copy by hand, or the app page
and the repository drift apart.

The private results repository is laid out as:

```
README.md                  index of every report and recording
runs/YYYY-MM-DD--HH-MM-SS/          one recording (measurement start, UTC)
reports/YYYY-MM-DD--HH-MM-SS/       an experiment report (creation time, UTC)
```

Each folder has `README.md` and `charts/*.png`, which GitHub renders on
the web and in its app, plus `report.html` (the same report as one page).
Recordings also keep `result.json` and `provenance.json`.

PNG publication needs `rsvg-convert` (`librsvg2-bin` on Debian/Ubuntu).
Charts use double-size text and 2.5× the previous height, rasterized at 3×
resolution (2700 pixels wide). Legends wrap within the image and show the
line and marker styles; charts with more than five series vary both styles.
Chart sources pass the credential/IP scan before conversion. A missing
converter leaves the existing published charts intact.

Each per-second download plot has two y-axes: individual client Mbit/s on
the left and summed client Mbit/s on the right. The black dashed total
uses samples at the same timestamp and has a gap if any client lacks a
sample. Both axes scale independently; the per-second total is distinct
from the ladder's overlap-averaged rate.

The root README has one summary line per experiment, followed by the report
and recording indexes. Each summary states the test scope, measured rung count,
highest observed total throughput and failures. Missing throughput is never zero.
Historical recordings without a measurement start use their creation timestamp;
original IDs remain in `result.json` for provenance.

Results are pushed automatically by `ladder`, `sweep` and `execute` as
soon as they exist, before any machine is destroyed; run `results push`
by hand only to retry or to publish several runs together. A single
run's report is named after its creation timestamp, so later pushes update it and
keep links attached with `results link`. A push of several runs gets a
report named after the earliest creation timestamp; `--report-name` overrides it.

Existing recordings are matched by their source identity before publication,
so a later push updates a renamed archive rather than recreating its old folder.
Live multi-cycle experiments retain their distinct stable slot names while pending;
the completed archive can use timestamp-only names once recording starts are known.

`results push` adds the given runs' recordings and one report over all of
them: one line per exit in the overview chart, and error bars (min to max
around the mean) where the same exit, network, profile and client package
were recorded more than once with matching measurement settings and origin
region. Different settings remain separate series.

For recordings with experiment metadata, the report has a schedule matrix in
cycle and exit order. Each recording shows its own ladder totals, overlap,
start skew, actual gaps and reconnect notes before the per-client details.
The machine inventory lists every origin and client, including CPU model and
topology, RAM, architecture, virtualization, root filesystem capacity, OS,
kernel and installed client versions. Older recordings show `n/a` for facts
that were not collected. The result contract defines the optional
[`run.experiment` and machine fields](result-schema.md).

Other commands:

```bash
./gnosis_vpn-bench results combine REC1 REC2 ... [--title T]   # report over recordings already in the repo
./gnosis_vpn-bench results link REPORT "Claude app" https://claude.ai/artifact/…
./gnosis_vpn-bench results rebuild                             # regenerate every page after a renderer change
```

Every command scans all generated files and refuses private keys, tokens
and VM or other public IP addresses, writes only allow-listed file names,
and refuses public repositories. It does not ask before writing,
committing and pushing (`--ask` adds a prompt; `--dry-run` only scans).
It never creates the results repository; create it on GitHub as
**private** yourself.

## Status at any time

```bash
./gnosis_vpn-bench status RUN
```

Shows the steps done, the droplets from the inventory, and where the funds
are, from the transaction journal: "sent" is confirmed; a transfer that was
broadcast without a receipt yet (or not found) shows as "sent, receipt
pending (reconcile)" and counts as sent, as it does for teardown; a planned
transfer with no journal entry shows as "planned, not sent".

## Historical results

The October 2026 ladder-repo outcomes can be converted with:

```bash
python3 -m gvpn_bench.results.historical ~/gnosis-vpn-serbia-ladder ~/gnosis-vpn-lithuania-ladder
```

They are marked `reproducible: false`; none reached ladder rung 1.
