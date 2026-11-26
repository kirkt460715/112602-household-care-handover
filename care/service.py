"""关怀服务后端的应用核心。

设计要点：
- 授权规则带版本与有效时段：撤回只收口当前版本，历史知会保留当时依据。
- 设备以时段归属事件：暂停、更换后的离线记录回到对应设备与时段。
- 离线只是“未确认”，不会被写成“已确认需要帮助”。
- 每份知会记录实际去向；跟进动作永远依据当前有效规则。
- 事件关闭必须有明确完成范围与依据，值守员不能随意关闭。
"""

from __future__ import annotations

from threading import RLock
from typing import Any, Optional

from .model import (
    DETAIL_LEVELS,
    VALID_CHANNELS,
    VALID_SCOPES,
    CareError,
    CareEvent,
    ContactRule,
    ContactWindow,
    Device,
    DeviceState,
    EventStatus,
    Notification,
    NotificationStatus,
    Receipt,
)

RECEIPT_RESULTS = {"accepted", "cannot_attend", "verified_safe"}


class CareService:
    """线程安全的内存服务，可快照到 JSON 文件。"""

    def __init__(self, resident_id: Optional[str] = None):
        self._lock = RLock()
        self.resident_id = resident_id
        self.windows: dict[str, ContactWindow] = {}
        self.rules: list[ContactRule] = []
        self.devices: dict[str, Device] = {}
        self.device_states: dict[str, list[DeviceState]] = {}
        self.device_evidence: dict[str, list[dict[str, Any]]] = {}
        self.events: dict[str, CareEvent] = {}
        self.notifications: dict[str, Notification] = {}
        self.ingested_sequences: set[int] = set()
        self._rule_seq = 0
        self._event_seq = 0
        self._notice_seq = 0

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _new_rule_id(self) -> str:
        self._rule_seq += 1
        return f"r-{self._rule_seq}"

    def _new_event_id(self) -> str:
        self._event_seq += 1
        return f"e-{self._event_seq}"

    def _new_notification_id(self) -> str:
        self._notice_seq += 1
        return f"n-{self._notice_seq}"

    def _window_for(self, contact: str) -> ContactWindow:
        window = self.windows.get(contact)
        if window is None:
            raise CareError("contact_unknown", f"未登记的联络窗口: {contact}")
        return window

    def _active_rule(self, contact: str, scope: str, at: int) -> Optional[ContactRule]:
        for rule in reversed(self.rules):
            if (
                rule.contact == contact
                and rule.scope == scope
                and rule.covers(at)
            ):
                return rule
        return None

    def _eligible_contacts(self, scope: str, at: int) -> list[ContactRule]:
        """当前时刻对某情形仍有效、且联络窗口可用的规则，按登记顺序。"""
        found: list[ContactRule] = []
        seen: set[str] = set()
        for rule in self.rules:
            if rule.scope != scope or not rule.covers(at):
                continue
            window = self.windows.get(rule.contact)
            if window is None or not window.covers(at):
                continue
            if rule.contact in seen:
                # 同一联系人保留最新版本（rules 按版本递增排列）
                found = [item for item in found if item.contact != rule.contact]
            found.append(rule)
            seen.add(rule.contact)
        return found

    def _get_event(self, event_id: str) -> CareEvent:
        event = self.events.get(event_id)
        if event is None:
            raise CareError("event_not_found", f"未知事件: {event_id}")
        return event

    def _get_notification(self, notification_id: str) -> Notification:
        notice = self.notifications.get(notification_id)
        if notice is None:
            raise CareError("notification_not_found", f"未知知会: {notification_id}")
        return notice

    def _active_notification(self, event_id: str, contact: str) -> Optional[Notification]:
        for notice in self.notifications.values():
            if (
                notice.event_id == event_id
                and notice.contact == contact
                and notice.status not in (
                    NotificationStatus.SUPERSEDED,
                    NotificationStatus.VERIFIED_SAFE,
                )
            ):
                return notice
        return None

    @staticmethod
    def _packet(notice: Notification) -> dict[str, Any]:
        """按该联系人可透露内容上限裁剪知会包，设备详情只在授权内出现。"""
        packet: dict[str, Any] = {
            "event_id": notice.event_id,
            "scope": notice.scope,
            "detail": notice.disclosed_detail,
        }
        if DETAIL_LEVELS[notice.disclosed_detail] >= DETAIL_LEVELS["call_request"]:
            packet["channel"] = notice.channel
        if notice.disclosed_detail == "device_detail":
            packet["device_ref"] = notice.event_id  # 仅在授权透露设备详情时给出
        return packet

    # ------------------------------------------------------------------
    # 住户：登记与授权
    # ------------------------------------------------------------------
    def register_resident(
        self,
        resident_id: str,
        windows: list[dict[str, Any]],
        rules: list[dict[str, Any]],
        at: int,
    ) -> dict[str, Any]:
        with self._lock:
            self.resident_id = resident_id
            for row in windows or []:
                self.add_window(row, at)
            for row in rules or []:
                self.grant_rule(row, at)
            return self.resident_ledger()

    def add_window(self, row: dict[str, Any], at: int) -> dict[str, Any]:
        with self._lock:
            contact = row.get("contact", "").strip()
            channel = row.get("channel", "")
            if not contact:
                raise CareError("invalid_contact", "缺少联系人标识")
            if channel not in VALID_CHANNELS:
                raise CareError("invalid_channel", f"未知联络渠道: {channel}")
            valid_from = int(row.get("valid_from", at))
            valid_to = row.get("valid_to")
            if valid_to is not None and valid_to < valid_from:
                raise CareError("invalid_period", "联络窗口结束早于开始")
            self.windows[contact] = ContactWindow(
                contact=contact,
                channel=channel,
                valid_from=valid_from,
                valid_to=valid_to,
            )
            return self.windows[contact].to_dict()

    def grant_rule(self, row: dict[str, Any], at: int) -> dict[str, Any]:
        with self._lock:
            contact = row.get("contact", "").strip()
            scope = row.get("scope", "")
            detail = row.get("detail", "")
            if not contact:
                raise CareError("invalid_contact", "缺少联系人标识")
            if scope not in VALID_SCOPES:
                raise CareError("invalid_scope", f"未知情形: {scope}")
            if detail not in DETAIL_LEVELS:
                raise CareError("invalid_detail", f"未知可透露内容: {detail}")
            self._window_for(contact)  # 必须先有联络窗口
            valid_from = int(row.get("valid_from", at))
            valid_to = row.get("valid_to")
            if valid_to is not None and valid_to < valid_from:
                raise CareError("invalid_period", "授权结束早于开始")
            # 同一联系人 + 情形若有现行版本，新版本先收口旧版本
            current = self._active_rule(contact, scope, valid_from)
            if current is not None:
                if current.valid_to is None:
                    current.valid_to = valid_from
            version = 1
            if current is not None:
                version = current.version + 1
            rule = ContactRule(
                rule_id=self._new_rule_id(),
                contact=contact,
                scope=scope,
                detail=detail,
                valid_from=valid_from,
                version=version,
                valid_to=valid_to,
            )
            self.rules.append(rule)
            return rule.to_dict()

    def revoke_contact(self, contact: str, at: int) -> dict[str, Any]:
        """联系人撤回：收口其全部现行规则，在途知会标记失效但保留去向。

        规则可能已因台账中的 until 自然到期，此时不再报“无授权”，
        仍须把在途知会标记为失效并记录撤回动作。
        """
        with self._lock:
            if contact not in self.windows:
                raise CareError("contact_unknown", f"未登记的联络窗口: {contact}")
            revoked: list[dict[str, Any]] = []
            for rule in self.rules:
                if rule.contact == contact and rule.valid_to is None:
                    rule.valid_to = at
                    revoked.append(rule.to_dict())
            affected: list[str] = []
            for notice in self.notifications.values():
                if (
                    notice.contact == contact
                    and notice.status
                    not in (NotificationStatus.SUPERSEDED, NotificationStatus.VERIFIED_SAFE)
                ):
                    notice.status = NotificationStatus.SUPERSEDED
                    event = self.events[notice.event_id]
                    event.record(
                        "authorization_revoked",
                        at,
                        contact=contact,
                        notification_id=notice.notification_id,
                    )
                    affected.append(notice.notification_id)
            if not revoked and not affected:
                raise CareError("no_active_authorization", "该联系人没有现行授权或在途知会")
            return {"revoked_at": at, "rules": revoked, "superseded": affected}

    def resident_ledger(self) -> dict[str, Any]:
        with self._lock:
            return {
                "resident": self.resident_id,
                "windows": [item.to_dict() for item in self.windows.values()],
                "rules": [item.to_dict() for item in self.rules],
                "devices": [item.to_dict() for item in self.devices.values()],
            }

    def contact_history(self) -> list[dict[str, Any]]:
        """住户可查已经做过的全部联系（含撤回与回执），按时间排序。"""
        with self._lock:
            items = list(self.notifications.values())
            items.sort(key=lambda notice: (notice.at, notice.notification_id))
            return [notice.to_dict() for notice in items]

    # ------------------------------------------------------------------
    # 设备：报到、暂停、更换
    # ------------------------------------------------------------------
    def register_device(self, device_id: str, at: int, valid_to: Optional[int] = None) -> dict[str, Any]:
        with self._lock:
            if device_id in self.devices:
                raise CareError("device_exists", f"设备已登记: {device_id}")
            device = Device(device_id=device_id, valid_from=at, valid_to=valid_to)
            self.devices[device_id] = device
            self.device_states.setdefault(device_id, [])
            return device.to_dict()

    def pause_device(self, device_id: str, at: int) -> dict[str, Any]:
        with self._lock:
            device = self.devices.get(device_id)
            if device is None:
                raise CareError("device_not_found", f"未知设备: {device_id}")
            if device.valid_to is not None:
                raise CareError("device_retired", "设备已结束使用，不能暂停")
            device.paused = True
            self._append_state(device_id, DeviceState(at=at, status="paused"))
            return device.to_dict()

    def replace_device(self, old_id: str, new_id: str, at: int) -> dict[str, Any]:
        """换新设备：旧设备时段在 at 收口，新设备自 at 生效，时段允许重叠。"""
        with self._lock:
            old = self.devices.get(old_id)
            if old is None:
                raise CareError("device_not_found", f"未知设备: {old_id}")
            if old.valid_to is None:
                old.valid_to = at
            if new_id in self.devices:
                raise CareError("device_exists", f"设备已登记: {new_id}")
            new = Device(device_id=new_id, valid_from=at)
            self.devices[new_id] = new
            self.device_states.setdefault(new_id, [])
            return {"retired": old.to_dict(), "registered": new.to_dict()}

    def _device_at(self, device_id: str, at: int) -> Optional[Device]:
        device = self.devices.get(device_id)
        if device is not None and device.covers(at):
            return device
        # 重叠时段下按时刻回溯对应设备
        for candidate in self.devices.values():
            if candidate.device_id == device_id and candidate.covers(at):
                return candidate
        return None

    def _append_state(self, device_id: str, state: DeviceState) -> None:
        self.device_states.setdefault(device_id, []).append(state)

    def record_device_state(
        self,
        device_id: str,
        at: int,
        status: str,
        last_seen: Optional[int] = None,
    ) -> dict[str, Any]:
        """记录设备回报。离线只产生“未确认”事件，绝不写成已确认需要帮助。"""
        with self._lock:
            if status not in ("online", "offline"):
                raise CareError("invalid_device_status", f"未知设备状态: {status}")
            device = self._device_at(device_id, at)
            if device is None:
                # 未知设备的回报仅留痕，不产生待办
                self.device_states.setdefault(device_id, []).append(
                    DeviceState(at=at, status=status, last_seen=last_seen, note="unknown_device")
                )
                return {"device_id": device_id, "event_id": None, "note": "unknown_device"}
            state = DeviceState(at=at, status=status, last_seen=last_seen)
            self._append_state(device_id, state)
            if status == "online":
                return {"device_id": device_id, "event_id": None, "note": "online_recorded"}
            if device.paused:
                state.note = "suppressed_paused"
                return {"device_id": device_id, "event_id": None, "note": "suppressed_paused"}
            # 同一设备同一未关闭事件不重复建单
            for event in self.events.values():
                if (
                    event.device_id == device_id
                    and event.status not in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED)
                ):
                    event.observations.append(state)
                    event.record("offline_repeated", at, last_seen=last_seen)
                    return {
                        "device_id": device_id,
                        "event_id": event.event_id,
                        "note": "joined_open_event",
                    }
            event = CareEvent(
                event_id=self._new_event_id(),
                device_id=device_id,
                opened_at=at,
                status=EventStatus.UNCONFIRMED,
                observations=[state],
                care_state="unconfirmed_offline",
            )
            event.record("opened_unconfirmed", at, last_seen=last_seen)
            self.events[event.event_id] = event
            return {"device_id": device_id, "event_id": event.event_id, "note": "opened_unconfirmed"}

    def attach_device_evidence(self, device_id: str, evidence: dict[str, Any], at: int) -> None:
        with self._lock:
            self.device_evidence.setdefault(device_id, []).append({"at": at, "evidence": evidence})

    # ------------------------------------------------------------------
    # 值守台：派单
    # ------------------------------------------------------------------
    def dispatch(
        self,
        event_id: str,
        contact: str,
        at: int,
        requested_detail: str,
        notification_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """按当前规则派出知会；透露内容不得超过该联系人的授权上限。"""
        with self._lock:
            event = self._get_event(event_id)
            if event.status in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                raise CareError("event_closed", "事件已结束，不能再派单")
            window = self._window_for(contact)
            if not window.covers(at):
                raise CareError("window_unavailable", "联络窗口当前不可用")
            rule = self._active_rule(contact, "unconfirmed_check", at)
            if rule is None:
                raise CareError("not_authorized", "当前规则不允许联系该联系人")
            if requested_detail not in DETAIL_LEVELS:
                raise CareError("invalid_detail", f"未知可透露内容: {requested_detail}")
            if not rule.permits(requested_detail):
                raise CareError(
                    "disclosure_forbidden",
                    f"授权上限为 {rule.detail}，不能透露 {requested_detail}",
                )
            if self._active_notification(event_id, contact) is not None:
                raise CareError("notification_active", "该联系人已有在途知会")
            notice_id = notification_id or self._new_notification_id()
            if notice_id in self.notifications:
                raise CareError("notification_exists", f"知会编号已存在: {notice_id}")
            notice = Notification(
                notification_id=notice_id,
                event_id=event_id,
                contact=contact,
                channel=window.channel,
                scope=rule.scope,
                disclosed_detail=requested_detail,
                at=at,
                status=NotificationStatus.DISPATCHED,
                rule_id=rule.rule_id,
                rule_version=rule.version,
            )
            self.notifications[notice_id] = notice
            event.notification_ids.append(notice_id)
            event.status = EventStatus.ACTIVE
            event.current_owner = contact
            event.care_state = "contact_dispatched"
            event.handover_note = event.handover_note or f"等待 {contact} 回执"
            event.record(
                "dispatched",
                at,
                contact=contact,
                notification_id=notice_id,
                rule_id=rule.rule_id,
                rule_version=rule.version,
                detail=requested_detail,
            )
            return {"notification": notice.to_dict(), "packet": self._packet(notice)}

    # ------------------------------------------------------------------
    # 合作点：回执
    # ------------------------------------------------------------------
    def receive_receipt(
        self,
        notification_id: str,
        result: str,
        at: int,
        accepted_at: Optional[int] = None,
        identity: Optional[str] = None,
        evidence: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        with self._lock:
            notice = self._get_notification(notification_id)
            if result not in RECEIPT_RESULTS:
                raise CareError("invalid_receipt", f"未知回执类型: {result}")
            event = self._get_event(notice.event_id)
            if event.status in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                raise CareError("event_closed", "事件已结束，回执仅可归档查询")
            receipt = Receipt(
                result=result,
                at=at,
                accepted_at=accepted_at,
                identity=identity or notice.contact,
                evidence=evidence,
            )
            notice.receipts.append(receipt)
            event.record(
                "receipt",
                at,
                notification_id=notification_id,
                result=result,
                delayed=accepted_at is not None and accepted_at < at,
            )
            if result == "accepted":
                if notice.status != NotificationStatus.SUPERSEDED:
                    notice.status = NotificationStatus.ACCEPTED
                    event.current_owner = notice.contact
                    event.care_state = "contact_accepted"
                    event.handover_note = f"{notice.contact} 已接手，等待到场核实"
                else:
                    # 撤回后才到达的接手回执：记录去向，但不恢复授权，
                    # 负责人挂空，等待值守台决定维持原安排或接手核对。
                    event.current_owner = None
                    event.care_state = "late_accept_after_revoke"
                    event.handover_note = f"{notice.contact} 的接手回执晚于撤回，待值守台判定"
                    event.record("late_accept_pending_decision", at, contact=notice.contact)
                return {"notification_id": notification_id, "effect": "recorded", "late": notice.status == NotificationStatus.SUPERSEDED}
            if result == "cannot_attend":
                if notice.status != NotificationStatus.SUPERSEDED:
                    notice.status = NotificationStatus.CANNOT_ATTEND
                event.current_owner = None
                event.care_state = "cannot_attend_needs_followup"
                self._route_after_failure(event, notice, at)
                return {"notification_id": notification_id, "effect": "routed_or_pending"}
            # verified_safe：核实平安是明确的完成依据
            notice.status = NotificationStatus.VERIFIED_SAFE
            event.status = EventStatus.VERIFIED_SAFE
            event.current_owner = notice.contact
            event.care_state = "verified_safe"
            event.close_basis = "verified_safe_receipt"
            event.close_refs.append(notification_id)
            event.record("verified_safe", at, notification_id=notification_id)
            for other in self.notifications.values():
                if other.event_id == event.event_id and other.notification_id != notice.notification_id:
                    if other.status not in (NotificationStatus.VERIFIED_SAFE,):
                        other.status = NotificationStatus.SUPERSEDED
            return {"notification_id": notification_id, "effect": "verified_safe", "event_id": event.event_id}

    def _route_after_failure(self, event: CareEvent, failed: Notification, at: int) -> None:
        """无法到场后按当前规则选择下一联系人；已撤联系人不会再被补发。"""
        tried = {
            notice.contact
            for notice in self.notifications.values()
            if notice.event_id == event.event_id
            and notice.notification_id != failed.notification_id
            and notice.status in (
                NotificationStatus.DISPATCHED,
                NotificationStatus.ACCEPTED,
                NotificationStatus.CANNOT_ATTEND,
                NotificationStatus.VERIFIED_SAFE,
            )
        }
        tried.add(failed.contact)
        for rule in self._eligible_contacts("unconfirmed_check", at):
            if rule.contact in tried:
                continue
            notice = Notification(
                notification_id=self._new_notification_id(),
                event_id=event.event_id,
                contact=rule.contact,
                channel=self.windows[rule.contact].channel,
                scope=rule.scope,
                disclosed_detail=rule.detail,
                at=at,
                status=NotificationStatus.DISPATCHED,
                rule_id=rule.rule_id,
                rule_version=rule.version,
            )
            self.notifications[notice.notification_id] = notice
            event.notification_ids.append(notice.notification_id)
            event.current_owner = rule.contact
            event.care_state = "rerouted"
            event.handover_note = f"原联系人无法到场，改派 {rule.contact}"
            event.record(
                "rerouted",
                at,
                contact=rule.contact,
                notification_id=notice.notification_id,
                after=failed.notification_id,
            )
            return
        event.handover_note = "无可用后续联系人，等待值守站处理"
        event.record("no_contact_available", at)

    # ------------------------------------------------------------------
    # 撤回后的迟到回执处置
    # ------------------------------------------------------------------
    def decide_late_receipt(self, event_id: str, contact: str, decision: str, at: int) -> dict[str, Any]:
        """邻居曾接手但回执晚到时，值守台判断其是否仍可完成原安排。

        - keep_original：确认其仍可完成，负责人保持不变；
        - station_takeover：值守站继续核对，并暂停向已撤联系人补发。
        """
        with self._lock:
            event = self._get_event(event_id)
            if decision not in ("keep_original", "station_takeover"):
                raise CareError("invalid_decision", f"未知处置: {decision}")
            notice = self._active_notification(event_id, contact)
            late_accept = any(
                item.contact == contact
                and item.status == NotificationStatus.SUPERSEDED
                and any(receipt.result == "accepted" for receipt in item.receipts)
                for item in self.notifications.values()
            )
            if notice is None and not late_accept:
                raise CareError("no_late_receipt", "该联系人没有可处置的迟到接手回执")
            if decision == "keep_original":
                if notice is None:
                    raise CareError(
                        "authorization_gone",
                        "授权已撤回且无在途知会，不能维持原安排",
                    )
                event.current_owner = contact
                event.care_state = "original_arrangement_kept"
                event.record("late_receipt_kept", at, contact=contact)
            else:
                event.status = EventStatus.STATION_VERIFYING
                event.current_owner = "desk"
                event.care_state = "station_verifying"
                event.handover_note = "值守站核对中；暂停向已撤联系人补发"
                event.record("station_takeover", at, contact=contact)
            return event.to_dict()

    # ------------------------------------------------------------------
    # 交班：同一事件保留负责动作、必要信息与关怀状态
    # ------------------------------------------------------------------
    def handover(
        self,
        event_id: str,
        to_party: str,
        at: int,
        role: str,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        """交班只交接当前负责动作与必要信息，不重发全部住户信息。

        role=desk 为值守台内部交班；role=partner 为合作点接手，
        合作点只能拿到自己名下的在途任务与裁剪后的知会包。
        """
        with self._lock:
            event = self._get_event(event_id)
            if event.status in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                raise CareError("event_closed", "事件已结束，无需交班")
            snapshot = {
                "event_id": event.event_id,
                "device_id": event.device_id,
                "status": event.status.value,
                "current_owner": event.current_owner,
                "care_state": event.care_state,
                "handover_note": note or event.handover_note,
                "open_notifications": [
                    item.notification_id
                    for item in self.notifications.values()
                    if item.event_id == event_id
                    and item.status not in (
                        NotificationStatus.SUPERSEDED,
                        NotificationStatus.VERIFIED_SAFE,
                    )
                ],
            }
            if role == "partner":
                notice = self._active_notification(event_id, to_party)
                if notice is None:
                    raise CareError("not_assigned", "该合作点没有此事件的在途任务")
                event.current_owner = to_party
                event.care_state = event.care_state or "with_partner"
                event.record("handover_partner", at, to=to_party)
                return {
                    "event_id": event.event_id,
                    "task": notice.to_dict(),
                    "packet": self._packet(notice),
                }
            if role != "desk":
                raise CareError("invalid_role", "role 只能是 desk 或 partner")
            event.current_owner = to_party
            event.record("handover_desk", at, to=to_party, note=note)
            snapshot["current_owner"] = to_party
            snapshot["handover_note"] = note or event.handover_note
            return snapshot

    # ------------------------------------------------------------------
    # 值守台核实与关闭
    # ------------------------------------------------------------------
    def station_verify(self, event_id: str, at: int, basis: str, refs: Optional[list[str]] = None) -> dict[str, Any]:
        with self._lock:
            event = self._get_event(event_id)
            if event.status in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                raise CareError("event_closed", "事件已结束")
            if not basis.strip():
                raise CareError("basis_required", "核实依据不能为空")
            event.status = EventStatus.STATION_VERIFYING
            event.current_owner = "desk"
            event.care_state = "station_verifying"
            event.record("station_verify", at, basis=basis, refs=refs or [])
            return event.to_dict()

    def resident_confirm_safe(self, event_id: str, at: int, basis: str = "resident_self_confirm") -> dict[str, Any]:
        """住户本人确认平安，同样是明确的完成依据。"""
        with self._lock:
            event = self._get_event(event_id)
            if event.status in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                raise CareError("event_closed", "事件已结束")
            event.status = EventStatus.VERIFIED_SAFE
            event.current_owner = self.resident_id
            event.care_state = "verified_safe"
            event.close_basis = basis
            event.close_refs.append("resident")
            event.record("resident_confirm_safe", at)
            for notice in self.notifications.values():
                if notice.event_id == event_id and notice.status != NotificationStatus.VERIFIED_SAFE:
                    notice.status = NotificationStatus.SUPERSEDED
            return event.to_dict()

    def close_event(self, event_id: str, at: int, basis: str, refs: Optional[list[str]] = None) -> dict[str, Any]:
        """值守员只能关闭完成范围明确的提醒。"""
        with self._lock:
            event = self._get_event(event_id)
            if event.status == EventStatus.CLOSED:
                raise CareError("event_closed", "事件已关闭")
            if not basis.strip():
                raise CareError("basis_required", "关闭必须写明完成范围与依据")
            if not refs:
                raise CareError("refs_required", "关闭必须附上依据编号（回执/核实记录）")
            if event.status != EventStatus.VERIFIED_SAFE and not basis.startswith("station_verified"):
                raise CareError(
                    "scope_unclear",
                    "没有核实平安或值守核实依据，完成范围不明确，不能关闭",
                )
            event.status = EventStatus.CLOSED
            event.close_basis = basis
            event.close_refs.extend(refs)
            event.record("closed", at, basis=basis, refs=refs)
            return event.to_dict()

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def list_events(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            items = sorted(self.events.values(), key=lambda event: (event.opened_at, event.event_id))
            if status is not None:
                try:
                    wanted = EventStatus(status)
                except ValueError:
                    raise CareError("invalid_status", f"未知事件状态: {status}")
                items = [event for event in items if event.status == wanted]
            return [event.to_dict() for event in items]

    def get_event(self, event_id: str) -> dict[str, Any]:
        with self._lock:
            return self._get_event(event_id).to_dict()

    def tasks_for(self, contact: str) -> list[dict[str, Any]]:
        """合作点只看自己名下的在途任务。"""
        with self._lock:
            result = []
            for notice in self.notifications.values():
                if notice.contact != contact:
                    continue
                if notice.status in (NotificationStatus.SUPERSEDED, NotificationStatus.VERIFIED_SAFE):
                    continue
                result.append(
                    {
                        "task": notice.to_dict(),
                        "packet": self._packet(notice),
                    }
                )
            return result

    # ------------------------------------------------------------------
    # 事实接入：把资料端到达记录翻译为本服务的命令
    # ------------------------------------------------------------------
    def ingest_fact(self, sequence: int, at: int, source: str, facts: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {"sequence": sequence, "applied": []}

        def apply(name: str, payload: Any) -> None:
            result["applied"].append({"command": name, "result": payload})

        with self._lock:
            if sequence in self.ingested_sequences:
                result["applied"].append({"command": "skip_duplicate", "result": {"sequence": sequence}})
                return result
            self.ingested_sequences.add(sequence)
            if source == "device":
                payload = self.record_device_state(
                    facts["id"],
                    at,
                    facts["status"],
                    last_seen=facts.get("last_seen"),
                )
                apply("record_device_state", payload)
            elif source == "desk":
                event = self._open_event_for_device_at(at)
                payload = self.dispatch(
                    event["event_id"],
                    facts["assigned"],
                    at,
                    facts["details"],
                    notification_id=facts.get("request"),
                )
                apply("dispatch", {"notification_id": payload["notification"]["notification_id"]})
            elif source == "resident":
                apply("revoke_contact", self.revoke_contact(facts["revoke"], at))
            elif source == "partner":
                payload = self.receive_receipt(
                    facts["request"],
                    facts["result"],
                    at,
                    accepted_at=facts.get("accepted_at"),
                    identity=facts.get("identity"),
                    evidence=facts.get("evidence"),
                )
                apply("receive_receipt", payload)
            else:
                raise CareError("unknown_source", f"未知事实来源: {source}")
            return result

    def _open_event_for_device_at(self, at: int) -> dict[str, Any]:
        """派单事实不直接带设备号时，找到该时刻应归属的未关闭事件。"""
        for event in sorted(self.events.values(), key=lambda item: item.opened_at):
            if event.status not in (EventStatus.VERIFIED_SAFE, EventStatus.CLOSED):
                return event.to_dict()
        raise CareError("no_open_event", "当前没有可派单的未关闭事件")

    # ------------------------------------------------------------------
    # 快照
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "resident": self.resident_id,
                "seq": {"rule": self._rule_seq, "event": self._event_seq, "notice": self._notice_seq},
                "windows": [item.to_dict() for item in self.windows.values()],
                "rules": [item.to_dict() for item in self.rules],
                "devices": [item.to_dict() for item in self.devices.values()],
                "device_states": {
                    key: [item.to_dict() for item in rows]
                    for key, rows in self.device_states.items()
                },
                "device_evidence": dict(self.device_evidence),
                "events": [item.to_dict() for item in self.events.values()],
                "notifications": [item.to_dict() for item in self.notifications.values()],
                "ingested_sequences": sorted(self.ingested_sequences),
            }

    def restore(self, data: dict[str, Any]) -> None:
        with self._lock:
            self.resident_id = data.get("resident")
            seq = data.get("seq", {})
            self._rule_seq = seq.get("rule", 0)
            self._event_seq = seq.get("event", 0)
            self._notice_seq = seq.get("notice", 0)
            self.windows = {
                row["contact"]: ContactWindow.from_dict(row) for row in data.get("windows", [])
            }
            self.rules = [ContactRule.from_dict(row) for row in data.get("rules", [])]
            self.devices = {
                row["device_id"]: Device.from_dict(row) for row in data.get("devices", [])
            }
            self.device_states = {
                key: [DeviceState.from_dict(row) for row in rows]
                for key, rows in data.get("device_states", {}).items()
            }
            self.device_evidence = {
                key: list(rows) for key, rows in data.get("device_evidence", {}).items()
            }
            self.events = {
                row["event_id"]: CareEvent.from_dict(row) for row in data.get("events", [])
            }
            self.notifications = {
                row["notification_id"]: Notification.from_dict(row)
                for row in data.get("notifications", [])
            }
            self.ingested_sequences = set(data.get("ingested_sequences", []))
