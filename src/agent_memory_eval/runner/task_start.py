"""Match the business input at the first worker decision of a freshly reset task."""

from dataclasses import dataclass
import re

from agent_memory_eval.contracts.core import ContractError, canonical, digest, read_json


FORMAT = "agent-memory-task-start/v1"


class TaskStartMismatch(ContractError):
    code = "TASK_START_MISMATCH"


@dataclass(frozen=True)
class TaskKey:
    benchmark: str
    revision: str
    task_id: str

    def __post_init__(self):
        if not all(isinstance(s, str) and s.strip() for s in (self.benchmark, self.revision, self.task_id)):
            raise TaskStartMismatch("A task start requires a benchmark, revision and task ID")


@dataclass(frozen=True)
class TaskStart:
    task: TaskKey
    controls_sha256: str
    public_input: bytes

    def __post_init__(self):
        if (not isinstance(self.task, TaskKey) or not isinstance(self.controls_sha256, str)
                or not re.fullmatch("[0-9a-f]{64}", self.controls_sha256)):
            raise TaskStartMismatch("Invalid task or reset fingerprint")
        if not isinstance(self.public_input, bytes):
            raise TaskStartMismatch("A task start holds immutable public input bytes")
        value = read_json(self.public_input)
        if not isinstance(value, dict) or not value or canonical(value) != self.public_input:
            raise TaskStartMismatch("The adapter must supply a canonical public business input")

    def record(self):
        return dict(format=FORMAT, benchmark=self.task.benchmark, revision=self.task.revision,
                    task_id=self.task.task_id, controls_sha256=self.controls_sha256,
                    public_input=read_json(self.public_input))

    @classmethod
    def from_record(cls, value):
        fields = {"format", "benchmark", "revision", "task_id", "controls_sha256", "public_input"}
        if not isinstance(value, dict) or set(value) != fields or value["format"] != FORMAT:
            raise TaskStartMismatch("Invalid task start record")
        return cls(TaskKey(value["benchmark"], value["revision"], value["task_id"]),
                   value["controls_sha256"], canonical(value["public_input"]))


def open_task_start(task, controls, prepare_input, *, reference=None):
    """Call once after native reset and before memory composition or the first worker call.

    The adapter owns the fresh session. It builds its public business input from that
    session and can reuse the reference's public observation when generation is required.
    Private controls are compared before giving the adapter any reference content.
    """
    if not isinstance(task, TaskKey) or not isinstance(controls, dict) or not controls:
        raise TaskStartMismatch("Task identity and native reset evidence are required")
    fingerprint = digest(canonical(controls))
    if reference is not None and (not isinstance(reference, TaskStart) or
                                  (reference.task, reference.controls_sha256) != (task, fingerprint)):
        raise TaskStartMismatch("Task identity or reset controls differ from the first pass")
    public = prepare_input(read_json(reference.public_input) if reference is not None else None)
    start = TaskStart(task, fingerprint, canonical(public))
    if reference is not None and start.public_input != reference.public_input:
        raise TaskStartMismatch("Public business input differs at the first worker decision")
    return start
