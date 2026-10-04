"""Alerts: something happened that a person should look at, not only a call that was refused.

An alert is an ERROR in the log and a `gateway.alert` record in the audit log, with a kind, the tool
and the client, and a count or a hash: never an argument or a result. The same condition is not
alerted again for a while (a tool whose description drifted is refused on every call, and an alert
for each would bury the first), though every refused call still has its own decision record.
"""

import logging
import time
from collections import OrderedDict
from collections.abc import Mapping

from ai_gateway.policy.audit import AuditRecorder
from ai_gateway.seams.events import GatewayEvent

logger = logging.getLogger(__name__)

REPEAT_AFTER_S = 600.0
_MAX_REMEMBERED = 2_048


class Alerts:
    def __init__(self, audit: AuditRecorder | None = None, repeat_after_s: float = REPEAT_AFTER_S):
        self._audit = audit
        self._repeat_after_s = repeat_after_s
        self._seen: OrderedDict[tuple[str, ...], float] = OrderedDict()

    def raise_alert(
        self,
        kind: str,
        *,
        tool: str | None,
        client: str | None,
        detail: Mapping[str, str | int],
    ) -> bool:
        """Raise the alert unless it was raised lately. Returns whether it was raised."""
        key = (kind, tool or "", client or "", *(f"{k}={v}" for k, v in sorted(detail.items())))
        now = time.monotonic()
        last = self._seen.get(key)
        if last is not None and now - last < self._repeat_after_s:
            return False
        self._seen[key] = now
        self._seen.move_to_end(key)
        while len(self._seen) > _MAX_REMEMBERED:
            self._seen.popitem(last=False)
        logger.error(
            "ALERT %s: tool %s, client %s, %s",
            kind,
            tool,
            client,
            ", ".join(f"{k}={v}" for k, v in sorted(detail.items())),
        )
        if self._audit is not None:
            self._audit.record(
                GatewayEvent(
                    action="gateway.alert",
                    actor_id="gateway",
                    subject_id=tool,
                    payload={
                        "kind": kind,
                        "tool": tool or "",
                        "client_name": client or "",
                        **dict(detail),
                    },
                )
            )
        return True
