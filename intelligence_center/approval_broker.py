"""Process-local bridge between approval HTTP requests and paused Agent runs."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = ""
    created_at: float = field(default_factory=time.time)


class ApprovalBroker:
    def __init__(self):
        self._lock = threading.RLock()
        self._pending: dict[str, _Pending] = {}

    def register(self, approval_id: str) -> None:
        with self._lock:
            self._pending.setdefault(str(approval_id), _Pending())

    def resolve(self, approval_id: str, decision: str) -> bool:
        if decision not in {"approved", "rejected"}:
            return False
        with self._lock:
            pending = self._pending.get(str(approval_id))
            if pending is None:
                return False
            pending.decision = decision
            pending.event.set()
            return True

    def wait(self, approval_id: str, timeout: float = 600) -> str:
        with self._lock:
            pending = self._pending.setdefault(str(approval_id), _Pending())
        if not pending.event.wait(max(1.0, float(timeout))):
            decision = "timeout"
        else:
            decision = pending.decision or "rejected"
        with self._lock:
            self._pending.pop(str(approval_id), None)
        return decision

    def is_pending(self, approval_id: str) -> bool:
        with self._lock:
            return str(approval_id) in self._pending


approval_broker = ApprovalBroker()

