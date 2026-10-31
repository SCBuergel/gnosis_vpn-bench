# `result.json` — schema version 1.0

Each run directory has one `result.json`. It is the only input to
`gnosis_vpn-bench report RUN` and the main file `results push` publishes.
The schema is defined in three places that tests keep in agreement:

| Form | Path |
|------|------|
| This document | `docs/result-schema.md` |
| JSON Schema (draft 2020-12) | `schema/result.schema.json` |
| Validator used by the tool (stdlib only) | `gvpn_bench/results/schema.py` |

## Rules

1. **A missing measurement is `null`, never `0`.** A rung that did not run,
   a client that never connected and an exit that failed all leave their
   rates `null` and give a `failure_status`/`status` from the fixed list
   below. A real 0 Mbit/s result would be a number; a missing one never is.
2. **Rates count only the common overlap.** For a ladder rung, every
   client's rate covers the same interval: from the *latest* transfer start
   to the *earliest* transfer end (completion, error or deadline). Tails
   outside that window are excluded.
3. **Exit addresses are lowercase `0x` hex.**
4. **Timestamps are ISO 8601 UTC** (`2026-10-29T16:01:44Z`).
5. **`schema_version` changes** when a field is renamed or removed, or its
   meaning changes. New optional fields do not change the version.

## Fixed status list

Used by `availability[].status`, `concurrent.rungs[].failure_status` and
`failures[].status`. `connected` is the only success value and cannot
appear in `failures`.

| Status | Meaning |
|--------|---------|
| `connected` | Tunnel established (and, for availability, the check ran) |
| `UnacceptablePixParams` | v5 session negotiation rejected by the exit's payment parameters |
| `DestinationNotFound` | The client does not know the exit address on this network |
| `http_404_register` | HTTP 404 from the exit at `/api/v1/clients/register` |
| `timeout` | Tunnel not established within the time limit |
| `control_timeout` | A `gnosis_vpn-ctl` request itself timed out |
| `worker_not_ready` | Worker never reached `Running` with destinations |
| `connect_rejected` | `connect` returned an error response |
| `route_bypass` | Measurement destination routed outside the tunnel; download skipped |
| `download_incomplete` | Download ended short of the expected bytes |
| `no_overlap` | Ladder transfers had no shared interval to average |
| `not_run` | Step skipped, usually because an earlier step failed |
| `error` | Anything else; `detail` says what |

## Top level

| Key | Type | Notes |
|-----|------|-------|
| `schema_version` | `"1.0"` | |
| `run` | object | What kind of run, when |
| `provenance` | object | Everything needed to compare runs |
| `concurrent` | object or `null` | The 1→2→3 ladder; `null` if this run had none |
| `per_client` | array | One entry per client machine (or local host) |
| `availability` | array | One row per exit check (sweep) |
| `failures` | array | Everything that went wrong, with status |

### `run`

| Key | Type | Notes |
|-----|------|-------|
| `id` | string | Run directory name |
| `kind` | `ladder` / `sweep` / `local` | |
| `created_at`, `started_at`, `finished_at` | timestamp (start/finish nullable) | |
| `reproducible` | bool | `false` for imported historical data whose raw inputs are incomplete |
| `notes` | string | Free text; may be empty |
| `historical_source` | string or null (optional) | Origin of imported data |

### `provenance`

| Key | Notes |
|-----|-------|
| `bench.version`, `bench.commit` | Tool version and `git describe` of the checkout |
| `profile.name` | e.g. `v4-jura-prod` |
| `profile.network` | HOPR network the client joined |
| `profile.channel` | `stable` / `experimental` |
| `profile.package_version`, `profile.package_sha256` | Pinned .deb |
| `profile.worker_model` | `v4-root`, `v5-user` or `local` |
| `client.package_version`, `client.ctl_version` | What was actually installed (`dpkg-query`, `gnosis_vpn-ctl --version`) |
| `target.name`, `target.label`, `target.exit_address`, `target.network`, `target.location` | From `targets/<name>.toml` |
| `infrastructure.provider` | `digitalocean`, `local` or `other` |
| `infrastructure.region` | e.g. `fra1` |
| `infrastructure.machines[]` | `role` (`origin`/`client`), `name`, `size`, `vcpus`, `memory_mib`, `cpu_model`, `os`, `kernel` |
| `client_config_sha256` | Optional; hash of an uploaded client config |

### `concurrent`

| Key | Notes |
|-----|-------|
| `spec.transfer_bytes` | 200,000,000 by default |
| `spec.deadline_s` | Shared per-rung deadline (300) |
| `spec.gap_s` | Gap between the end of one rung and the next start (10) |
| `spec.ping_target`, `spec.ping_interval_s` | `9.9.9.9`, 1 s |
| `origin` | `{bytes, sha256}` of the served file |
| `baseline[]` | Direct, non-VPN downloads: `{client, mbps, bytes}`. An origin-capacity check, not a VPN result |
| `rungs[]` | See below |

Each rung:

| Key | Notes |
|-----|-------|
| `rung` | 1, 2, 3 … |
| `clients` | Client names in this rung |
| `status` | `measured`, `failed` or `not_run` |
| `failure_status` | From the fixed list; required unless `measured` |
| `total_mbps` | Sum of per-client overlap rates; `null` unless `measured` |
| `overlap_s`, `start_skew_ms`, `gap_before_s` | Overlap length, spread of start times, actual gap since the previous rung |
| `overlap_start`, `overlap_end` | Optional timestamps |
| `per_client[]` | `client`, `mbps`, `overlap_bytes`, `downloaded_bytes`, `complete`, `sha256_verified`, `timed_out`, `ping_mean_ms`, `ping_loss_pct`, optional `ping_sent`, `ping_received`, `timeseries` (`{t_s[], mbps[], rtt_ms[]}`, per second since the scheduled start; `null` entries are gaps) |

### `per_client[]`

`client`, `connected` (bool), `connect_time_s`, optional `exit_address`,
`egress_changed` (public IP differs from the VM's own), and optional
`measurements[]` (`kind`: `download`/`upload`/`latency`, `label`,
`size_bytes`, `mbps`, `ms`, `at`).

### `availability[]`

`exit_address`, `exit_label`, `exit_id`, `location`, `client`, `status`,
`detail`, `download_bytes`, `download_mbps`, `ping_mean_ms`,
`ping_loss_pct`, `api_rtt_ms`, `tested_at`. `download_mbps` must be `null`
unless `status` is `connected`. These are short checks; their rates are not
overlap-normalised.

### `failures[]`

`scope` (`run`/`rung`/`client`/`exit`), `subject` (e.g. `rung 2`,
`client1`, an exit address), `status`, `detail`, `at`.

## Comparing runs

Runs are comparable when these match:

* `provenance.target.exit_address`
* `provenance.profile.network`
* `provenance.profile.name`

Add `provenance.client.package_version` to compare only identical client
builds. Order by `run.started_at`. Within a ladder, compare
`concurrent.rungs[].total_mbps` per `rung`; never compare availability-check
rates with ladder rates.

## Files next to `result.json`

| File | Published by `results push` |
|------|------------------------------|
| `result.json` | yes |
| `report.html` | yes (rendered from `result.json`) |
| `provenance.json` | yes (copy of `result.provenance`) |
| `run.toml`, `state/`, `data/`, logs | never |
