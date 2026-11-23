from types import SimpleNamespace

import httpx
import pytest

from canary import controller
from canary.analysis import Verdict, load_policy

BASELINE = "v1.4.0"
CANDIDATE = "v1.4.1"


@pytest.fixture
def local_rollout(tmp_path, monkeypatch):
    monkeypatch.setattr(controller, "STATE", tmp_path)
    monkeypatch.setattr(controller, "annotate", lambda *_: None)
    monkeypatch.setattr(controller, "recreate", lambda _: None)
    monkeypatch.setattr(controller, "wait_ready", lambda *_: None)
    monkeypatch.setattr(controller, "time", SimpleNamespace(monotonic=controller.time.monotonic, sleep=lambda _: None))
    controller.cmd_init(BASELINE)
    return tmp_path


@pytest.mark.parametrize("error_type", [httpx.ConnectError, KeyboardInterrupt])
def test_deploy_rolls_back_after_an_interrupted_analysis(local_rollout, monkeypatch, error_type):
    def fail_with_weighted_canary(_):
        assert (local_rollout / "canary_weight").read_text() == "10"
        raise error_type("analysis interrupted")

    monkeypatch.setattr(controller.Prometheus, "firing_alerts", fail_with_weighted_canary)

    with pytest.raises(error_type, match="analysis interrupted"):
        controller.cmd_deploy(CANDIDATE, skip_evals=True)

    assert (local_rollout / "canary_weight").read_text() == "0"
    assert (local_rollout / "canary.env").read_text().startswith(f"RELEASE={BASELINE}\n")
    assert controller.read_state() == {"stable": BASELINE, "canary": None, "history": [BASELINE]}


def use_immediate_analysis(monkeypatch, analyse):
    policy = load_policy()
    policy["step_duration_s"] = 0
    monkeypatch.setattr(controller, "load_policy", lambda: policy)
    monkeypatch.setattr(controller, "analyse", analyse)


def test_failed_promotion_restores_stable_before_returning_its_traffic(local_rollout, monkeypatch):
    use_immediate_analysis(monkeypatch, lambda *_: Verdict("pass"))
    stable_releases_checked = []

    def check_ready(track, release):
        if track == "stable":
            assert (local_rollout / "canary_weight").read_text() == "100"
            stable_releases_checked.append(release)
            if release == CANDIDATE:
                raise RuntimeError("candidate failed to start on stable")

    monkeypatch.setattr(controller, "wait_ready", check_ready)

    with pytest.raises(RuntimeError, match="candidate failed to start"):
        controller.cmd_deploy(CANDIDATE, skip_evals=True)

    assert stable_releases_checked == [CANDIDATE, BASELINE]
    assert (local_rollout / "stable.env").read_text().startswith(f"RELEASE={BASELINE}\n")
    assert (local_rollout / "canary_weight").read_text() == "0"
    assert controller.read_state() == {"stable": BASELINE, "canary": None, "history": [BASELINE]}


def test_failed_manual_cleanup_cannot_reenable_canary_traffic(local_rollout, monkeypatch):
    def fail_container_reset(_):
        raise RuntimeError("failed to recreate canary")

    def cancel_during_analysis(*_):
        with monkeypatch.context() as patch:
            patch.setattr(controller, "recreate", fail_container_reset)
            with pytest.raises(RuntimeError, match="failed to recreate canary"):
                controller.cmd_rollback()
        return Verdict("pass")

    use_immediate_analysis(monkeypatch, cancel_during_analysis)

    assert controller.cmd_deploy(CANDIDATE, skip_evals=True) == 1
    assert (local_rollout / "canary_weight").read_text() == "0"
    assert controller.read_state()["canary"] == CANDIDATE
    assert controller.cmd_rollback() == 0
    assert controller.read_state()["canary"] is None


def test_healthy_candidate_is_promoted_and_can_be_reverted(local_rollout, monkeypatch):
    use_immediate_analysis(monkeypatch, lambda *_: Verdict("pass"))

    assert controller.cmd_deploy(CANDIDATE, skip_evals=True) == 0
    assert controller.read_state() == {"stable": CANDIDATE, "canary": None, "history": [BASELINE, CANDIDATE]}
    assert (local_rollout / "canary_weight").read_text() == "0"

    assert controller.cmd_rollback() == 0
    assert controller.read_state() == {"stable": BASELINE, "canary": None, "history": [BASELINE]}


def test_matching_release_must_also_be_ready(monkeypatch):
    clock = iter([0, 0, 3])
    monkeypatch.setattr(controller, "time", SimpleNamespace(monotonic=lambda: next(clock), sleep=lambda _: None))

    def get(url, *, headers, timeout):
        assert headers == {"X-Canary": "always"}
        if url.endswith("/version"):
            return httpx.Response(200, json={"release": CANDIDATE})
        assert url.endswith("/readyz")
        return httpx.Response(503)

    monkeypatch.setattr(controller.httpx, "get", get)

    with pytest.raises(RuntimeError, match="canary did not become ready"):
        controller.wait_ready("canary", CANDIDATE, timeout_s=2)
