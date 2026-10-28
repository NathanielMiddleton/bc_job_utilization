"""Load config.yml from the app root and fill in defaults.

PyYAML is used when available; otherwise a minimal parser handles the flat
`key: value` file this app ships with, so the app has no hard dependencies
beyond the Python standard library.
"""
import os

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULTS = {
    "cluster_name": "",          # display only
    "cluster": None,             # passed as -M when set (multi-cluster slurmdbd)
    "sacct": "sacct",
    "sreport": "sreport",
    "sinfo": "sinfo",
    "scontrol": "scontrol",
    "command_timeout": 120,      # seconds
    "default_hours": 24,
    "max_days": 92,              # longest custom range the UI may request
    "max_jobs_in_table": 2000,   # summary uses all jobs; only the table is capped
    "allow_all_users": True,     # hide the "everyone"/partition scopes if False
    "dashboard_url": "/pun/sys/dashboard",
    # TRES to ask sreport about for the cluster-wide panel. Add typed GPUs
    # (e.g. gres/gpu:h200) when slurm.conf's AccountingStorageTRES tracks them.
    "cluster_tres": ["cpu", "gres/gpu"],
    "hints": True,               # show optimization hints (jobutil/advice.py)
    "gpu_total": None,           # override: total GPUs for occupancy when sinfo is unavailable
}


def _parse_flat_yaml(text):
    out = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip() if not line.lstrip().startswith("#") else ""
        if ":" not in line or line.startswith((" ", "\t")):
            continue
        key, val = line.split(":", 1)
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        elif val == "":
            val = None
        elif val.lower() in ("true", "yes", "on"):
            val = True
        elif val.lower() in ("false", "no", "off"):
            val = False
        elif val.lower() in ("null", "~"):
            val = None
        elif val.startswith("[") and val.endswith("]"):
            val = [x.strip().strip("\"'") for x in val[1:-1].split(",") if x.strip()]
        else:
            try:
                val = int(val)
            except ValueError:
                pass
        out[key.strip()] = val
    return out


def load():
    cfg = dict(DEFAULTS)
    path = os.path.join(APP_ROOT, "config.yml")
    if os.path.exists(path):
        with open(path) as fh:
            text = fh.read()
        try:
            import yaml
            data = yaml.safe_load(text) or {}
        except ImportError:
            data = _parse_flat_yaml(text)
        cfg.update({k: v for k, v in data.items() if v is not None})
    return cfg
