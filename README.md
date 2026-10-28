# Job Utilization — an Open OnDemand app for Slurm

Shows how efficiently finished Slurm jobs used what they were allocated
(CPU, memory, GPU, wall-time limit) and how much they consumed (CPU-, node-,
GPU- and GiB-hours), for a selectable time window that defaults to the last
24 hours. Scopes: your own jobs, everyone's, or one partition; a cluster-wide
capacity panel (allocated / idle / down) comes from `sreport`.

The web app itself is Python standard library only (3.8+), so it has nothing
to install. It can be deployed two ways from this one tree:

* **Batch Connect app** (this directory) — launched from *Interactive Apps*,
  it runs as a one-core Slurm job on a compute node and OnDemand proxies the
  browser to it. Nothing is installed on the web node, and it can live in a
  personal sandbox (`~/ondemand/dev/`) or in `/var/www/ood/apps/sys/`.
* **System (Passenger) app** — `template/app/` on its own is a complete
  Passenger app that runs inside the per-user NGINX on the web node.

Either way every Slurm command runs **as the logged-in user** with that
user's accounting permissions.

```
job-utilization/                 ← Batch Connect app root
├── manifest.yml                 role: batch_connect
├── form.yml                     cluster id, modules, hours, partition
├── submit.yml.erb               one core, 2 GB
├── view.html.erb                "Open Job Utilization" button (posts the session token)
├── icon.png / icon.svg          app icon (OnDemand picks up icon.png automatically)
└── template/                    copied into each session's staging directory
    ├── before.sh.erb            picks a free port and a random token
    ├── script.sh.erb            loads modules, starts server.py on the compute node
    └── app/                     ← the web app; also a complete Passenger app on its own
        ├── server.py            WSGI app, routes, token login, standalone server
        ├── passenger_wsgi.py    Passenger entry point (system-app deployment)
        ├── manifest.yml         system-app metadata (not used by Batch Connect)
        ├── config.yml           site settings (paths, limits, cluster name)
        ├── jobutil/             slurm.py (sacct/sreport/sinfo), parse.py, metrics.py, config.py
        ├── templates/index.html, static/app.js, static/app.css
        ├── dev.sh               run locally against fake Slurm output
        └── tests/               pytest suite + fixtures + fake sacct/sreport/sinfo
```

## Install as a Batch Connect app

Requirements: compute nodes with `python3` ≥ 3.8 and the Slurm client
commands (`sacct`, `sreport`, `sinfo`) on the PATH (or available via a module
you name in the form); `node_uri`/`rnode_uri` enabled in `ood_portal.yml` and
a `host_regex` matching your compute hostnames — the same prerequisites as
Jupyter or RStudio under OnDemand.

```bash
# Personal sandbox (developer mode must be enabled for your account):
cp -r job-utilization ~/ondemand/dev/job-utilization
# — or site-wide (as root):
sudo cp -r job-utilization /var/www/ood/apps/sys/job-utilization
```

Then edit `form.yml` and set `cluster:` to the id of a file in
`/etc/ood/config/clusters.d/`. Optional: `template/app/config.yml` for the
cluster display name and limits. In OnDemand choose *Help → Restart Web
Server*; the app appears under *Interactive Apps* (sandbox apps under *My
Sandbox Apps*). Launch it, wait for the job to start, and click
*Open Job Utilization* on the session card.

How it fits together: `before.sh.erb` exports a free `port` and a random
`password`; `script.sh.erb` passes them to `server.py` as `JOBUTIL_TOKEN` and
sets `JOBUTIL_URL_PREFIX=/node/<host>/<port>` so links resolve under the
OnDemand proxy. The `/node` proxy only requires an OnDemand login — not
ownership of the session — so the session card posts the token to `/login`,
which exchanges it for an HttpOnly cookie; every other request without that
cookie gets 403. The dashboard job appears in your own job list afterwards;
the notes panel says so.

## Install as a system (Passenger) app

Requirements: OnDemand's PUN Python (`passenger_python`) ≥ 3.8 and the Slurm
client commands on the web node.

```bash
sudo cp -r job-utilization/template/app /var/www/ood/apps/sys/job-utilization
sudo $EDITOR /var/www/ood/apps/sys/job-utilization/config.yml
```

*Help → Restart Web Server*; the app appears under *Reports → Slurm* at
`/pun/sys/job-utilization`. No token is set in this mode, because the PUN has
already authenticated the user and runs the app as them.

Notes for both modes:

* The icon is `icon.png` in the app root (source in `icon.svg`): three
  utilization bars, the lowest highlighted. OnDemand uses an `icon.png` when
  present; to use a Font Awesome glyph instead, add `icon: fa://chart-bar`
  back to `manifest.yml`.

* Chart.js is loaded by the browser from cdnjs. If users' browsers cannot
  reach the internet, download `chart.umd.js` into `static/` and change the
  `<script>` tag in `templates/index.html`; the page falls back to text
  summaries if the library does not load.
* PyYAML is used for `config.yml` when present; otherwise a built-in parser
  reads the flat `key: value` file.
* Development without a cluster: `template/app/dev.sh` serves on port 8080
  using the fake commands in `tests/bin`, which replay
  `tests/fixtures/sacct_sample.txt` with timestamps relative to now.
  `python3 -m pytest tests` runs the suite from `template/app/`.

## What the numbers mean

| Metric | Formula | Source |
|---|---|---|
| CPU efficiency | `TotalCPU / (ElapsedRaw × AllocCPUS)` | job record |
| Memory efficiency | `max over steps of TRESUsageInTot mem / allocated memory` | steps; falls back to `MaxRSS` |
| Allocated memory | `AllocTRES mem=` (total allocated, unambiguous); else `ReqMem` with the `n`/`c` suffixes of older Slurm | job record |
| GPU-hours | `gres/gpu in AllocTRES × elapsed` | job record |
| GPU type | typed TRES `gres/gpu:<type>=N`; any remainder vs `gres/gpu` is "untyped" | job record |
| GPU utilization | mean over steps of `gres/gpuutil` from `TRESUsageInAve` (else `…Max`, else `…Tot` for single-task steps); typed keys accepted | only when slurmdbd tracks `gres/gpuutil` |
| Wall-time use | `ElapsedRaw / (TimelimitRaw × 60)` | job record |
| CPU-/node-/GiB-hours | allocation × elapsed (× in-window fraction when prorating) | job record |
| Cluster capacity | `sreport cluster utilization -t Seconds` (cpu and gres/gpu) | slurmdbd hourly roll-ups |

"Weighted" efficiencies in the headline bars are hour-weighted
(`Σ used / Σ allocated`), so one big wasteful job counts more than many tiny
ones; the per-job mean is shown alongside.

### GPU types

`AllocTRES=cpu=8,mem=50G,node=1,billing=8,gres/gpu=1,gres/gpu:h200=1` is
recorded as one GPU of type `h200`. Types are lower-cased, a job can carry
several (`a100 ×1, h200 ×1`), and GPUs with no typed entry are labelled
`untyped`. The type appears as a job-table column, GPU-hours are split by
type in the GPU ledger row, and a *GPU jobs by GPU type* table gives per-type
GPU-hours, GPU utilization and CPU/memory efficiency — so a one-off node such
as a single H200 is visible on its own rather than folded into "GPU". If the
typed entries do not add up to `gres/gpu`, the total is used for GPU-hours and
a note says so.

For the cluster-wide capacity panel, list the typed TRES in
`config.yml` → `cluster_tres` (e.g. `[cpu, gres/gpu, gres/gpu:h200]`). sreport
can only report a TRES that slurm.conf's `AccountingStorageTRES` tracks; an
untracked one is skipped with a note rather than failing the panel.

## Drilling into the charts

Every bar is a filter. Clicking a bin of the CPU- or memory-efficiency
histogram, a partition bar, or a row in the by-partition / by-user /
by-account / by-GPU-type tables narrows the job table to exactly those jobs
and scrolls to it; the other bars dim, a chip above the table says what is
selected, and clicking again, the ✕ on the chip, or Escape clears it. The
text filter composes with the selection. Hovering a bar lists the first few
job IDs in the tooltip. Without Chart.js the bins render as buttons and work
the same way. Note that the table holds at most `max_jobs_in_table` jobs
while the histogram counts all of them, so on very large windows a bin can
show more jobs than the drill-down lists.

## Optimization hints

`jobutil/advice.py` turns the results into suggestions, shown in a *What to
try* section and as short tags in the job table (hover for the reasoning).
Cards are ordered by how much allocated time they could recover and each
includes an `#SBATCH` snippet where one applies plus a caveat about what the
accounting data cannot see.

| Rule | Fires when | Suggests |
|---|---|---|
| Fewer cores | ≥ 2 cores, CPU efficiency < 50%, job ≥ 10 min, not a GPU job | `--cpus-per-task` = used cores × 1.25; flags serial-looking jobs (≤ 1.3 cores' worth of CPU time) |
| Less memory | efficiency < 30%, request ≥ 4 GiB, job ≥ 10 min | `--mem` = peak × 1.3 (marked ≈ when the peak is MaxRSS) |
| Out of memory / near limit | `OUT_OF_MEMORY`, or > 95% of request used | 1.5× the failed request; add headroom |
| Shorter time limit | < 25% of a ≥ 30 min limit used | `--time` = longest actual run × 1.5, rounded up to 15 min (backfill) |
| Hit time limit | `TIMEOUT` | raise `--time`; checkpoint with `--signal=B:USR1@300` and requeue |
| Idle GPU | gres/gpuutil < 30% | CPU-bound (busy cores) → more cores / loader workers; otherwise I/O or batch size |
| GPU utilization unknown | GPU jobs without gres/gpuutil | `nvidia-smi dmon` for users; `AccountingStorageTRES=gres/gpuutil` for admins |
| Bundle short jobs | ≥ 20 jobs under 5 min | array tasks that each process a chunk, or GNU parallel |
| Failures | ≥ 5 failed and ≥ 20% of jobs | test one case first; how to read exit codes |
| Cancelled late | `CANCELLED` after ≥ 1 h | check early output / scale down first |
| Where the idle hours are | everyone / partition scope only | top users by idle CPU-hours, for targeted outreach |

Thresholds are constants at the top of `advice.py`; set `hints: false` in
`config.yml` to hide the section entirely.

## Multi-day jobs and window boundaries

A finished job is a single accounting record, so a window shorter than the
job cannot "see" part of it. The app offers two explicit attribution modes
and tells the user what each hides:

* **ended in** (default) — a job belongs to the window it finished in and all
  of its consumption is attributed there. A 5-day job that ended this morning
  adds 5 days of CPU-hours to today's view. The notes panel reports how many
  jobs started before the window and how many CPU-hours they spent before it
  opened, plus how many jobs ran in the window but are excluded because they
  finished later or are still running (and their in-window CPU-hours).
* **ran during** — every job overlapping the window is included and its
  consumption is multiplied by the share of its run time that fell inside the
  window (shown as a `prorated NN%` badge). Efficiency ratios stay whole-job,
  because Slurm does not record usage per hour. Running jobs can optionally be
  included; their efficiency is "so far", not final.

Jobs longer than 24 h carry a `multi-day` badge, jobs that started before the
window `began earlier`, and jobs that finished after it or are still running
`ends later`.

Other disclaimers the app raises automatically:

* **Other users' jobs not returned** — if `sacct -a` only returns the caller's
  own jobs, slurmdbd's `PrivateData=jobs` is hiding the rest; the everyone and
  partition views are then incomplete (operators/admins are unaffected).
* **Memory peaks above 100%** — `TRESUsageInTot mem` is the sum of RSS over
  every process in a step, so forked workers re-count pages shared with
  their parent and the sum can exceed what the node held; alternatively the
  cluster does not enforce memory and the job really exceeded its request.
  Such jobs are flagged (`mem over`), the raw value is shown per job, and the
  aggregate memory efficiency caps each job at 100% so a few of them cannot
  push a user or partition above 100%. On clusters that enforce memory,
  `JobAcctGatherParams=UsePss` or `jobacct_gather/cgroup` records accurate
  peaks. The allocation side uses `AllocTRES mem` because some Slurm
  21.08–22.x releases report `ReqMem` per CPU for `--mem-per-cpu` jobs
  without any suffix, which alone produces efficiencies of several hundred
  percent.
* **Memory from MaxRSS** — when `TRESUsageInTot` is empty the app uses
  `MaxRSS`, which is the largest *single task*; multi-task steps are
  under-reported.
* **No memory measurement** — jobs with no steps (e.g. plain `salloc`), or
  clusters without `JobAcctGatherType`, report no usage.
* **GPU utilization unavailable** — when no GPU job in the window has a
  `gres/gpuutil` value, the app inspects one job's raw `TRESUsageIn*` step
  fields and says which case applies: no step records (salloc without srun),
  no usage gathered at all (`JobAcctGatherType` off), or usage present but
  `gres/gpuutil` not in `AccountingStorageTRES` (the usual one; add
  `gres/gpu,gres/gpumem,gres/gpuutil`, restart slurmctld, and only jobs that
  finish afterwards will carry it). The raw fields are shown under the notes
  with the `sacct` command to reproduce them. GPU-hours are always available.
* **sreport granularity** — the capacity panel rounds to whole hours and
  typically lags up to an hour behind.
* **Deliberately no `--state` to sacct** — combined with `-S/-E`, sacct's
  state filter means "was in this state during the window" and silently drops
  jobs still running inside the window that finished later. State filtering
  is done in `metrics.select_jobs` instead.

## Configuration (`template/app/config.yml`)

| Key | Default | Purpose |
|---|---|---|
| `cluster_name` | `""` | shown in the header |
| `cluster` | unset | passed as `-M` to Slurm commands |
| `sacct`, `sreport`, `sinfo` | command names | full paths if not on the PUN's PATH |
| `command_timeout` | 120 | seconds before a Slurm command is abandoned |
| `default_hours` | 24 | initial window |
| `max_days` | 92 | longest custom range (protects slurmdbd) |
| `max_jobs_in_table` | 2000 | table cap; summaries always use all jobs |
| `allow_all_users` | true | hide the everyone/partition scopes when false |
| `dashboard_url` | `/pun/sys/dashboard` | back link |
| `cluster_tres` | `[cpu, gres/gpu]` | TRES for the sreport capacity panel; add typed GPUs like `gres/gpu:h200` |
| `hints` | true | show optimization hints and per-job hint tags |

## API

All endpoints return JSON and accept `start`/`end` (ISO 8601, local time).
In Batch Connect mode they require the session cookie from `POST /login`.

* `GET api/jobs?scope=mine|all|partition&partition=…&mode=ended|prorate&states=finished|completed|unsuccessful&running=0|1`
  → `window`, `summary`, `info` (boundary counters), `by_partition`, `by_user`,
  `by_account`, `by_gpu_type`, `jobs[]` (each with `gpu_type` and `hints[]`), `advice[]`, `warnings[]`
* `GET api/cluster` → sreport totals per TRES
* `GET api/partitions`

Inputs are validated (`^[A-Za-z0-9_.@+-]{1,64}$` for names) and commands run
with argument lists, never a shell.
