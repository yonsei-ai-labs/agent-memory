"""Small runtime types; experimental settings are always explicit."""

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from .validation import canonical, digest, pointer, read_json


def new_id(prefix):
    return prefix + "_" + uuid4().hex


class ContractError(ValueError):
    pass


class MemoryFault(ContractError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RunContext:
    experiment_id: str
    run_id: str | None = None
    session_id: str | None = None
    condition_id: str | None = None
    stage: str = "controller"
    phase: str = "prepare"
    record_kind: str = "illustrative"

    def at(self, phase):
        return replace(self, phase=phase)

    def event(self, kind, payload, causes=()):
        return dict(schema_version="1.1", event_id=new_id("e"), event_seq=0,
                    **asdict(self), type=kind, payload=payload, caused_by=list(causes),
                    timestamp_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))


@dataclass(frozen=True)
class MemoryPolicy:
    enabled: bool
    write_timing: str
    same_episode_read: bool
    page_size: int
    read_mode: str = "on_demand"
    max_items: int | None = None
    max_payload_bytes: int | None = None
    max_return_bytes: int | None = None

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.same_episode_read) is not bool:
            raise ContractError("Memory flags must be booleans")
        if self.write_timing not in {"none", "during_task", "post_task", "during_and_post"}:
            raise ContractError("Unknown write timing")
        if self.read_mode not in {"none", "on_demand"}:
            raise ContractError("This first runtime supports verbatim on-demand reads only")
        if type(self.page_size) is not int or self.page_size < 1:
            raise ContractError("An explicit positive page size is required")
        if not self.enabled and (self.write_timing != "none" or self.read_mode != "none" or self.same_episode_read):
            raise ContractError("Disabled memory has no write/read policy")
        for value in (self.max_items, self.max_payload_bytes, self.max_return_bytes):
            if value is not None and (type(value) is not int or value < 0):
                raise ContractError("Memory limits are nonnegative integers or None")

    def permits(self, name, phase):
        if not self.enabled or phase not in {"solve", "post_task_write"}:
            return False
        if name == "memory_store":
            return self.write_timing in ({"during_task", "during_and_post"} if phase == "solve"
                                         else {"post_task", "during_and_post"})
        return name in {"memory_list", "memory_get"} and self.read_mode == "on_demand"

    def tools(self, phase):
        return [name for name in ("memory_store", "memory_list", "memory_get") if self.permits(name, phase)]
