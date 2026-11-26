"""通信关怀服务的领域模型。

模型只描述联络授权、设备时段、关怀事件与回执，不含任何健康诊断概念。
所有时间用整数时刻表示，服务本身不解释其单位（样例中为相对时序值）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class CareError(Exception):
    """业务规则拒绝，``code`` 稳定，供接口层映射为 HTTP 状态。"""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class EventStatus(str, Enum):
    UNCONFIRMED = "unconfirmed"          # 离线仅代表未确认，不等于需要帮助
    ACTIVE = "active"                    # 已派出联络，等待处理
    STATION_VERIFYING = "station_verifying"  # 值守站接手核对，暂停向已撤联系人补发
    VERIFIED_SAFE = "verified_safe"      # 已核实平安
    CLOSED = "closed"                    # 值守员按明确完成范围关闭


class NotificationStatus(str, Enum):
    DISPATCHED = "dispatched"
    ACCEPTED = "accepted"
    CANNOT_ATTEND = "cannot_attend"
    VERIFIED_SAFE = "verified_safe"
    SUPERSEDED = "superseded"            # 授权撤回或改派，此份知会不再有效


# 可透露内容由低到高。低层级详情属于高层级的子集：
# 能透露设备详情时，自然也能说明请求性质。
DETAIL_LEVELS = {
    "visit_request": 1,
    "call_request": 2,
    "device_detail": 3,
}

VALID_SCOPES = {"unconfirmed_check"}
VALID_STATUSES = set(EventStatus)
VALID_CHANNELS = {"self", "neighbor", "family", "partner_point", "desk"}


@dataclass
class ContactWindow:
    """住户登记的联络窗口（渠道与可用时段）。"""

    contact: str
    channel: str
    valid_from: int
    valid_to: Optional[int] = None  # None 表示持续有效

    def covers(self, at: int) -> bool:
        if at < self.valid_from:
            return False
        return self.valid_to is None or at <= self.valid_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "contact": self.contact,
            "channel": self.channel,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ContactWindow":
        return cls(
            contact=row["contact"],
            channel=row["channel"],
            valid_from=row["valid_from"],
            valid_to=row.get("valid_to"),
        )


@dataclass
class ContactRule:
    """可联系某人的情形（scope）与可透露内容上限（detail）。

    规则带版本号与有效时段，撤回即把当前版本收口，历史版本保留，
    已派出的知会仍能回溯当时的授权依据。
    """

    rule_id: str
    contact: str
    scope: str
    detail: str
    valid_from: int
    version: int
    valid_to: Optional[int] = None

    def covers(self, at: int) -> bool:
        if at < self.valid_from:
            return False
        return self.valid_to is None or at <= self.valid_to

    def permits(self, requested: str) -> bool:
        return DETAIL_LEVELS[requested] <= DETAIL_LEVELS[self.detail]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "contact": self.contact,
            "scope": self.scope,
            "detail": self.detail,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ContactRule":
        return cls(
            rule_id=row["rule_id"],
            contact=row["contact"],
            scope=row["scope"],
            detail=row["detail"],
            valid_from=row["valid_from"],
            valid_to=row.get("valid_to"),
            version=row["version"],
        )


@dataclass
class Device:
    """设备及其生效时段。暂停或更换都通过时段表达，历史可重叠。"""

    device_id: str
    valid_from: int
    valid_to: Optional[int] = None
    paused: bool = False

    def covers(self, at: int) -> bool:
        if at < self.valid_from:
            return False
        return self.valid_to is None or at <= self.valid_to

    @property
    def active(self) -> bool:
        return self.valid_to is None and not self.paused

    def to_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "paused": self.paused,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Device":
        return cls(
            device_id=row["device_id"],
            valid_from=row["valid_from"],
            valid_to=row.get("valid_to"),
            paused=row.get("paused", False),
        )


@dataclass
class DeviceState:
    """某设备在某时刻的在线事实与值守结论。"""

    at: int
    status: str                      # online / offline
    last_seen: Optional[int] = None
    note: Optional[str] = None       # 例如 suppressed_paused / suppressed_unknown

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "status": self.status,
            "last_seen": self.last_seen,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "DeviceState":
        return cls(
            at=row["at"],
            status=row["status"],
            last_seen=row.get("last_seen"),
            note=row.get("note"),
        )


@dataclass
class Notification:
    """一次实际派出的知会：发给谁、按哪条规则、透露了什么、落到哪。"""

    notification_id: str
    event_id: str
    contact: str
    channel: str
    scope: str
    disclosed_detail: str
    at: int
    status: NotificationStatus
    rule_id: str
    rule_version: int
    receipts: list["Receipt"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "event_id": self.event_id,
            "contact": self.contact,
            "channel": self.channel,
            "scope": self.scope,
            "disclosed_detail": self.disclosed_detail,
            "at": self.at,
            "status": self.status.value,
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "receipts": [item.to_dict() for item in self.receipts],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Notification":
        return cls(
            notification_id=row["notification_id"],
            event_id=row["event_id"],
            contact=row["contact"],
            channel=row["channel"],
            scope=row["scope"],
            disclosed_detail=row["disclosed_detail"],
            at=row["at"],
            status=NotificationStatus(row["status"]),
            rule_id=row["rule_id"],
            rule_version=row["rule_version"],
            receipts=[Receipt.from_dict(item) for item in row.get("receipts", [])],
        )


@dataclass
class Receipt:
    """合作点回执：已接手 / 无法到场 / 已核实平安。证据独立留存。"""

    result: str  # accepted / cannot_attend / verified_safe
    at: int
    accepted_at: Optional[int] = None
    identity: Optional[str] = None
    evidence: Optional[dict[str, Any]] = None
    source: str = "partner"

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result,
            "at": self.at,
            "accepted_at": self.accepted_at,
            "identity": self.identity,
            "evidence": self.evidence,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Receipt":
        return cls(
            result=row["result"],
            at=row["at"],
            accepted_at=row.get("accepted_at"),
            identity=row.get("identity"),
            evidence=row.get("evidence"),
            source=row.get("source", "partner"),
        )


@dataclass
class CareEvent:
    """一次未应答提醒从产生到关闭的全过程，始终归属于具体设备与时段。"""

    event_id: str
    device_id: str
    opened_at: int
    status: EventStatus
    observations: list[DeviceState] = field(default_factory=list)
    notification_ids: list[str] = field(default_factory=list)
    current_owner: Optional[str] = None
    care_state: Optional[str] = None          # 关怀状态文本，交班时原样保留
    handover_note: Optional[str] = None       # 保留的必要信息
    close_basis: Optional[str] = None
    close_refs: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def record(self, action: str, at: int, **extra: Any) -> None:
        entry = {"action": action, "at": at}
        entry.update(extra)
        self.history.append(entry)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "device_id": self.device_id,
            "opened_at": self.opened_at,
            "status": self.status.value,
            "observations": [item.to_dict() for item in self.observations],
            "notification_ids": list(self.notification_ids),
            "current_owner": self.current_owner,
            "care_state": self.care_state,
            "handover_note": self.handover_note,
            "close_basis": self.close_basis,
            "close_refs": list(self.close_refs),
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "CareEvent":
        return cls(
            event_id=row["event_id"],
            device_id=row["device_id"],
            opened_at=row["opened_at"],
            status=EventStatus(row["status"]),
            observations=[DeviceState.from_dict(item) for item in row.get("observations", [])],
            notification_ids=list(row.get("notification_ids", [])),
            current_owner=row.get("current_owner"),
            care_state=row.get("care_state"),
            handover_note=row.get("handover_note"),
            close_basis=row.get("close_basis"),
            close_refs=list(row.get("close_refs", [])),
            history=list(row.get("history", [])),
        )
