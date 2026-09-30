"""Shipped scenarios and fault families stay clean on a spread of seeds."""

import pytest

from helpers import run

FAMILIES = ["network", "crashes", "clocks", "gpu", "soak"]


@pytest.mark.parametrize("seed", range(1, 13))
def test_soak_clean(seed):
    _, violations, summary = run("soak", seed=seed)
    assert violations == [], violations[:3]
    assert all(a["outcome"] != "unresolved" for a in summary["actions"])


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", [2, 5, 8])
def test_base_with_fault_family_clean(family, seed):
    from c3sim import runner
    from helpers import ROOT
    import os
    cb, res = runner.run(ROOT, "base", seed=seed,
                         faults=os.path.join(ROOT, "scenarios", "faults", family + ".yaml"))
    v = runner.check_result(cb, res)
    assert v == [], v[:3]


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_public_template_clean(seed):
    _, violations, _ = run("public-template", seed=seed)
    assert violations == [], violations[:3]


def test_replay_is_deterministic():
    from c3sim import runner
    from helpers import ROOT
    a = runner.run(ROOT, "soak", seed=4)[1].sha256()
    b = runner.run(ROOT, "soak", seed=4)[1].sha256()
    assert a == b
