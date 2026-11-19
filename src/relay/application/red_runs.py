"""Run the registered acceptance contract against a baseline and judge the result."""

from __future__ import annotations

import tempfile
from pathlib import Path

from relay.application.behaviours import Behaviours
from relay.domain.evidence import Execution, RedVerdict
from relay.git.checkout import pristine_checkout
from relay.testing.oracle import ProcessOracle


class RedRuns:
    def __init__(self, behaviours: Behaviours, oracle: ProcessOracle) -> None:
        self._behaviours = behaviours
        self._oracle = oracle

    async def run(
        self, project_id: str, behaviour_id: str, repository: str, baseline_revision: str
    ) -> RedVerdict:
        contract = await self._behaviours.contract_of(project_id, behaviour_id)
        with tempfile.TemporaryDirectory(prefix="relay-red-") as scratch:
            workdir = await pristine_checkout(
                repository, baseline_revision, Path(scratch) / "subject"
            )
            observation = await self._oracle.observe(workdir, contract.launch)
        return await self._behaviours.record_red_run(
            project_id,
            behaviour_id,
            Execution(baseline_revision, self._oracle.isolation.name, observation),
        )
