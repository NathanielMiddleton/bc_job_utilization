import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import render_fixture                      # noqa: E402
from jobutil import capacity, metrics, parse, slurm      # noqa: E402
from jobutil.slurm import SACCT_FIELDS                   # noqa: E402

NOW = datetime(2026, 10, 28, 12, 0, 0)
CFG = {"sinfo": os.path.join(os.path.dirname(__file__), "bin", "sinfo"),
       "scontrol": os.path.join(os.path.dirname(__file__), "bin", "scontrol"), "command_timeout": 10}


@pytest.fixture
def setup():
    rows = parse.sacct_rows(render_fixture("sacct_sample.txt", NOW), SACCT_FIELDS)
    return slurm.node_inventory(CFG), metrics.build_jobs(rows)


def test_hostlist_expansion():
    assert slurm.expand_hostlist("cpu[01-03]") == ["cpu01", "cpu02", "cpu03"]
    assert slurm.expand_hostlist("gpu[1,3-4],n7") == ["gpu1", "gpu3", "gpu4", "n7"]
    assert slurm.expand_hostlist("None assigned") == []
    assert slurm.expand_hostlist("r[1-2]n[1-2]") == ["r1n1", "r1n2", "r2n1", "r2n2"]


def test_node_inventory_marks_down_and_merges_partitions(setup):
    nodes, _ = setup
    by = {n["name"]: n for n in nodes}
    assert by["gpu03"]["partitions"] == {"gpu", "debug"}
    assert by["gpu02"]["down"] and "xid" in by["gpu02"]["reason"]
    assert by["cpu03"]["down"] and not by["cpu01"]["down"]
    assert by["cpu01"]["features"] == ["epyc", "ib"]


def test_cpu_breakdown_by_partition_and_feature(setup):
    nodes, jobs = setup
    bd = capacity.breakdown(nodes, jobs, NOW - timedelta(hours=24), NOW, "cpu", NOW)
    parts = {g["name"]: g for g in bd["groups"]["partition"]}
    # cpu partition capacity: 3 nodes x 128 cores x 24 h
    assert parts["cpu"]["capacity"] == pytest.approx(3 * 128 * 24)
    # allocated in cpu partition: 1001(4x2) 1003(16x23.5 in window) 1005/1006(0.5 each) 1007 running(32x4)
    #                              1009(2x5min) 1012(8x1) 1013(1x1)
    expected = 8 + 16 * 23.5 + 1 + 32 * 4 + 2 * 300 / 3600 + 8 + 1
    assert parts["cpu"]["allocated"] == pytest.approx(expected)
    assert parts["cpu"]["idle"] == pytest.approx(3 * 128 * 24 - expected)
    feats = {g["name"]: g for g in bd["groups"]["feature"]}
    # 1003 spans cpu01 (epyc,ib) and cpu02 (epyc): its hours split evenly
    assert feats["epyc,ib"]["allocated"] == pytest.approx(8 + 16 * 23.5 / 2 + 32 * 4 + 2 * 300 / 3600 + 8)
    assert feats["epyc"]["capacity"] == pytest.approx(2 * 128 * 24)        # cpu02 + cpu03


def test_gpu_breakdown_by_type_infers_untyped_from_node(setup):
    nodes, jobs = setup
    bd = capacity.breakdown(nodes, jobs, NOW - timedelta(hours=24), NOW, "gres/gpu", NOW)
    types = {g["name"]: g for g in bd["groups"]["gpu_type"]}
    assert types["a100"]["capacity"] == pytest.approx(8 * 24)
    assert types["h200"]["capacity"] == pytest.approx(1 * 24) and types["h200"]["allocated"] == pytest.approx(1.0)
    assert types["a100"]["allocated"] == pytest.approx(6.0)
    # an untyped GPU job on a single-type node gets that node's type
    j = metrics.Job(id="9", raw_id="9", name="n", user="u", account="a", partition="gpu", state="COMPLETED",
                    start=NOW - timedelta(hours=3), end=NOW - timedelta(hours=1), elapsed_s=7200, alloc_cpus=1,
                    alloc_nodes=1, cpu_used_s=0, mem_req_b=None, mem_alloc_source=None, mem_used_b=None,
                    mem_source=None, gpus=2, gpu_types={"": 2}, gpu_util=None, timelimit_s=None, exit_code="",
                    nodelist="gpu01")
    bd2 = capacity.breakdown(nodes, [j], NOW - timedelta(hours=24), NOW, "gres/gpu", NOW)
    t2 = {g["name"]: g for g in bd2["groups"]["gpu_type"]}
    assert t2["a100"]["allocated"] == pytest.approx(4.0) and "untyped" not in t2


def test_user_and_account_have_no_capacity(setup):
    nodes, jobs = setup
    bd = capacity.breakdown(nodes, jobs, NOW - timedelta(hours=24), NOW, "cpu", NOW)
    users = {g["name"]: g for g in bd["groups"]["user"]}
    assert users["alice"]["capacity"] is None and users["alice"]["idle"] is None
    assert users["bob"]["allocated"] == pytest.approx(1 + 32 * 4)         # array tasks + running job


def test_down_nodes_and_reservations(setup):
    nodes, _ = setup
    down = capacity.down_nodes(nodes, "gres/gpu")
    assert [d["name"] for d in down] == ["gpu02"] and down[0]["gpu_types"] == {"a100": 4}
    assert [d["name"] for d in capacity.down_nodes(nodes, "cpu")] == ["gpu02", "cpu03"]
    res = slurm.reservations(CFG)
    assert {r["name"] for r in res} == {"maint_window", "course_gpu"}
    assert res[0]["cores"] == 384 and res[1]["tres"] == "cpu=32,gres/gpu=1"
