"""Parsers for Slurm unit strings and for sacct / sreport parsable output.

Everything here is pure (no subprocesses) so it can be unit-tested against
captured command output.
"""
import re
from datetime import datetime

_MEM_MULT = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
_MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)([nc]?)$")
_TIME_FMT = "%Y-%m-%dT%H:%M:%S"
_UNSET = {"", "Unknown", "None", "N/A", "INVALID", "UNLIMITED", "Partition_Limit", "NOT_SET"}


def parse_mem(text, bare_unit="M"):
    """Parse a Slurm memory string ('4000M', '16Gn', '2Gc', '12345K').

    Returns (bytes, per) where per is None, 'n' (per node) or 'c' (per CPU).
    A bare number is interpreted in `bare_unit`. Returns (None, None) when
    the string is empty or unparseable.
    """
    if text is None:
        return None, None
    text = text.strip()
    if not text:
        return None, None
    m = _MEM_RE.match(text)
    if not m:
        return None, None
    num, unit, per = m.groups()
    return float(num) * _MEM_MULT[unit or bare_unit], (per or None)


def parse_duration(text):
    """Parse '[D-]HH:MM:SS', 'MM:SS' or 'MM:SS.mmm' into seconds (float).

    Returns None for empty/unset values ('UNLIMITED', 'Partition_Limit', ...).
    """
    if text is None:
        return None
    text = text.strip()
    if text in _UNSET:
        return None
    days = 0
    if "-" in text:
        d, text = text.split("-", 1)
        try:
            days = int(d)
        except ValueError:
            return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3:
        return None
    while len(parts) < 3:
        parts.insert(0, 0.0)
    h, m, s = parts
    return days * 86400 + h * 3600 + m * 60 + s


def parse_time(text):
    """Parse a Slurm timestamp ('2026-10-27T13:00:00'); None when unset.

    Timestamps are returned as naive datetimes in the local time zone, which
    is what sacct emits when SLURM_TIME_FORMAT=standard.
    """
    if text is None:
        return None
    text = text.strip()
    if text in _UNSET:
        return None
    try:
        return datetime.strptime(text, _TIME_FMT)
    except ValueError:
        return None


def parse_tres(text):
    """Parse 'cpu=4,mem=16G,node=1,gres/gpu=2' into a dict of raw strings."""
    out = {}
    if not text:
        return out
    for item in text.split(","):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def gpu_types(tres):
    """GPUs by type from an AllocTRES dict.

    'gres/gpu=1,gres/gpu:h200=1' -> {'h200': 1}
    'gres/gpu=4'                 -> {'': 4}        (untyped)
    'gres/gpu=3,gres/gpu:a100=2' -> {'a100': 2, '': 1}  (1 GPU of unknown type)
    Type names are lower-cased so H200 and h200 are the same bucket.
    """
    typed = {}
    for k, v in tres.items():
        if k.startswith("gres/gpu:"):
            name = k[len("gres/gpu:"):].strip().lower()
            if name:
                typed[name] = typed.get(name, 0) + _to_int(v)
    total = _to_int(tres["gres/gpu"]) if "gres/gpu" in tres else sum(typed.values())
    typed_total = sum(typed.values())
    if total > typed_total:
        typed[""] = total - typed_total
    elif total < typed_total and total > 0:
        # Typed entries claim more than the total: trust the total and keep
        # the type labels; the caller can flag the inconsistency.
        pass
    return typed


def gpu_count(tres):
    """Number of GPUs in an AllocTRES dict (gres/gpu=N, else the sum of typed entries)."""
    if "gres/gpu" in tres:
        return _to_int(tres["gres/gpu"])
    return sum(gpu_types(tres).values())


def gpu_type_label(types):
    """{'h200': 1} -> 'h200'; {'a100': 2, '': 1} -> 'a100 ×2, untyped ×1'; {} -> ''."""
    if not types:
        return ""
    parts = []
    for name, n in sorted(types.items(), key=lambda kv: (kv[0] == "", kv[0])):
        label = name or "untyped"
        parts.append(label if (len(types) == 1 and n == sum(types.values())) else f"{label} ×{n}")
    return ", ".join(parts)


def _to_int(text, default=0):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return default


def parse_state(text):
    """'CANCELLED by 1234' -> 'CANCELLED'."""
    return (text or "").split()[0] if text else ""


def sacct_rows(output, fields, delimiter="|"):
    """Split `sacct -P -n` output into dicts keyed by field name.

    The last field (JobName) may itself contain the delimiter, so the split is
    capped at len(fields)-1.
    """
    rows = []
    n = len(fields) - 1
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split(delimiter, n)
        if len(parts) < len(fields):
            parts += [""] * (len(fields) - len(parts))
        rows.append(dict(zip(fields, parts)))
    return rows


def sreport_table(output):
    """Parse `sreport -P cluster utilization` output into a list of dicts.

    sreport prints a banner before the header line, so the header is located
    by looking for the line that starts with 'Cluster|'. Column names differ
    across Slurm versions (Reserved vs Planned), so callers should look up
    columns by name with .get().
    """
    lines = [l for l in output.splitlines() if l.strip()]
    header = None
    rows = []
    for line in lines:
        if header is None:
            if line.startswith("Cluster|"):
                header = line.split("|")
            continue
        parts = line.split("|")
        if len(parts) == len(header):
            rows.append(dict(zip(header, parts)))
    return rows
