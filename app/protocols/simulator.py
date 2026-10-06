"""Built-in simulated camera driver.

Lets the whole app be exercised with no hardware: it fabricates a job that
counts up, occasionally fails a part, and periodically "resets" its raw
counters (to prove the reset-proof accumulation works) and changes job.

Configure via Device.protocol_config, all optional::

    {
        "jobs": ["JOB_A", "JOB_B"],
        "fail_ratio": 0.08,
        "parts_per_poll": 5,
        "reset_every": 12,     # reset raw counters every N reads
        "job_change_every": 40 # switch job every N reads
    }
"""
from __future__ import annotations

import random

from ..counters import Sample
from .base import ProtocolDriver

# module-level so repeated reads of the same device keep counting
_STATE: dict[int, dict] = {}


class SimulatorDriver(ProtocolDriver):
    key = "simulator"
    label = "Simulated device (no hardware)"
    config_fields = {
        "jobs": "List of job names to cycle through",
        "fail_ratio": "Fraction of parts that fail (0-1)",
        "parts_per_poll": "Parts produced between polls",
        "reset_every": "Reset raw counters every N reads (0 = never)",
        "job_change_every": "Switch job every N reads (0 = never)",
    }

    def _key(self) -> int:
        return id(self) if not self.config.get("device_id") else int(self.config["device_id"])

    def read(self) -> Sample:
        cfg = self.config
        jobs = cfg.get("jobs") or ["SIM_JOB"]
        fail_ratio = float(cfg.get("fail_ratio", 0.08))
        ppp = int(cfg.get("parts_per_poll", 5))
        reset_every = int(cfg.get("reset_every", 12))
        job_change_every = int(cfg.get("job_change_every", 40))

        st = _STATE.setdefault(self._key(), {"reads": 0, "job_idx": 0, "p": 0, "f": 0})
        st["reads"] += 1

        if job_change_every and st["reads"] % job_change_every == 0:
            st["job_idx"] = (st["job_idx"] + 1) % len(jobs)
            st["p"] = st["f"] = 0  # a real camera usually resets on job change

        if reset_every and st["reads"] % reset_every == 0:
            st["p"] = st["f"] = 0

        for _ in range(ppp):
            if random.random() < fail_ratio:
                st["f"] += 1
            else:
                st["p"] += 1

        return Sample(
            job_name=jobs[st["job_idx"]],
            raw_pass=st["p"],
            raw_fail=st["f"],
            extra={"simulated": True, "reads": st["reads"]},
        )
