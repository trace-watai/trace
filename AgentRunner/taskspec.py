"""Task specification (the task contract).

NOTE: The real TaskSpec schema is owned by another team (task authoring /
verifier). This is a deliberately small placeholder so the runner has something
concrete to consume. Widen these fields as the real schema lands; keep
``load_by_id`` as the seam.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class TaskSpec:
    task_id: str
    goal: str
    initial_state: dict = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    # Deterministic fixture script: a list of structured actions the fixture
    # model replays in order. Each action is one of:
    #   {"type": "tool", "name": str, "args": dict}
    #   {"type": "done", "message": str}
    #   {"type": "say",  "message": str}
    script: list[dict] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict) -> "TaskSpec":
        return cls(
            task_id=data["task_id"],
            goal=data.get("goal", ""),
            initial_state=data.get("initial_state", {}),
            allowed_tools=data.get("allowed_tools", []),
            script=data.get("script", []),
        )


def load_by_id(task_id: str, tasks_dir: str) -> TaskSpec:
    """Read a TaskSpec from ``{tasks_dir}/{task_id}.json``."""
    path = os.path.join(tasks_dir, f"{task_id}.json")
    with open(path, "r", encoding="utf-8") as fh:
        return TaskSpec.from_dict(json.load(fh))
