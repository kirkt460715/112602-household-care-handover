"""领域服务测试：授权、设备、派单、回执、撤回、交班与关闭规则。"""

import unittest

from care import CareError, CareService, EventStatus, NotificationStatus


def build_service(at: int = 0) -> CareService:
    service = CareService()
    service.register_resident(
        "care-3",
        windows=[
            {"contact": "neighbor-2", "channel": "neighbor", "valid_from": 0},
            {"contact": "family-6", "channel": "family", "valid_from": 0},
        ],
        rules=[
            {"contact": "neighbor-2", "scope": "unconfirmed_check", "detail": "visit_request"},
            {"contact": "family-6", "scope": "unconfirmed_check", "detail": "call_request"},
        ],
        at=at,
    )
    return service


class AuthorizationTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_disclosure_cannot_exceed_rule(self):
        self.service.register_device("d-1", 0)
        result = self.service.record_device_state("d-1", 10, "offline", last_seen=8)
        event_id = result["event_id"]
        # 邻居只授权 visit_request，不能拿到 call_request / device_detail
        with self.assertRaises(CareError) as raised:
            self.service.dispatch(event_id, "neighbor-2", 11, "call_request")
        self.assertEqual(raised.exception.code, "disclosure_forbidden")
        with self.assertRaises(CareError):
            self.service.dispatch(event_id, "neighbor-2", 11, "device_detail")
        packet = self.service.dispatch(event_id, "neighbor-2", 11, "visit_request")
        self.assertNotIn("device_ref", packet["packet"])
        self.assertNotIn("channel", packet["packet"])

    def test_family_gets_higher_detail_packet(self):
        self.service.register_device("d-1", 0)
        event_id = self.service.record_device_state("d-1", 10, "offline")["event_id"]
        packet = self.service.dispatch(event_id, "family-6", 11, "call_request")["packet"]
        self.assertEqual(packet["detail"], "call_request")
        self.assertIn("channel", packet)
        self.assertNotIn("device_ref", packet)  # call_request 仍不含设备详情

    def test_rule_revision_keeps_history(self):
        before = self.service.grant_rule(
            {"contact": "neighbor-2", "scope": "unconfirmed_check", "detail": "call_request"},
            at=20,
        )
        self.assertEqual(before["version"], 2)
        ledger = self.service.resident_ledger()
        neighbor_rules = [r for r in ledger["rules"] if r["contact"] == "neighbor-2"]
        self.assertEqual([r["version"] for r in neighbor_rules], [1, 2])
        self.assertEqual(neighbor_rules[0]["valid_to"], 20)
        self.assertIsNone(neighbor_rules[1]["valid_to"])

    def test_revocation_closes_rules_and_supersedes_notice(self):
        self.service.register_device("d-1", 0)
        event_id = self.service.record_device_state("d-1", 10, "offline")["event_id"]
        notice = self.service.dispatch(event_id, "neighbor-2", 11, "visit_request")["notification"]
        outcome = self.service.revoke_contact("neighbor-2", 15)
        self.assertEqual(outcome["superseded"], [notice["notification_id"]])
        # 撤回后 family-6 仍可派；邻居则不行
        self.service.dispatch(event_id, "family-6", 16, "call_request")
        with self.assertRaises(CareError):
            self.service.dispatch(event_id, "neighbor-2", 17, "visit_request")
        # 被撤回的知会仍保留实际去向与当时规则版本
        stored = self.service.contact_history()[0]
        self.assertEqual(stored["status"], NotificationStatus.SUPERSEDED.value)
        self.assertEqual(stored["rule_version"], 1)


class DeviceLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()

    def test_offline_is_unconfirmed_not_help_request(self):
        self.service.register_device("d-1", 0)
        result = self.service.record_device_state("d-1", 30, "offline", last_seen=28)
        event = self.service.get_event(result["event_id"])
        self.assertEqual(event["status"], EventStatus.UNCONFIRMED.value)
        self.assertEqual(event["care_state"], "unconfirmed_offline")
        self.assertNotIn("confirmed", event["close_basis"] or "")

    def test_paused_device_offline_creates_no_task(self):
        self.service.register_device("d-1", 0)
        self.service.pause_device("d-1", 5)
        result = self.service.record_device_state("d-1", 9, "offline")
        self.assertIsNone(result["event_id"])
        self.assertEqual(result["note"], "suppressed_paused")
        self.assertEqual(self.service.list_events(), [])

    def test_replacement_overlapping_periods_and_new_pending(self):
        # d-1 [0,50]，d-2 自 48 起，重叠时段各自归属
        self.service.register_device("d-1", 0, valid_to=50)
        self.service.replace_device("d-1", "d-2", 48)
        first = self.service.record_device_state("d-1", 46, "offline", last_seen=44)
        self.assertEqual(first["event_id"], "e-1")
        # 新设备在线不产生事项
        online = self.service.record_device_state("d-2", 49, "online")
        self.assertIsNone(online["event_id"])
        # 旧设备在其时段内再次离线，回到原设备的未关闭事件
        again = self.service.record_device_state("d-1", 49, "offline", last_seen=47)
        self.assertEqual(again["note"], "joined_open_event")
        self.assertEqual(again["event_id"], "e-1")
        # 新设备之后离线产生新的待处理事项
        second = self.service.record_device_state("d-2", 60, "offline", last_seen=58)
        self.assertEqual(second["event_id"], "e-2")
        events = self.service.list_events()
        self.assertEqual([e["device_id"] for e in events], ["d-1", "d-2"])

    def test_unknown_device_is_logged_not_dispatched(self):
        result = self.service.record_device_state("ghost", 3, "offline")
        self.assertIsNone(result["event_id"])
        self.assertEqual(result["note"], "unknown_device")


class NotificationReceiptTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.register_device("d-1", 0)
        self.event_id = self.service.record_device_state("d-1", 30, "offline", last_seen=28)["event_id"]
        self.notice = self.service.dispatch(self.event_id, "neighbor-2", 41, "visit_request", notification_id="c-19")
        self.notice_id = self.notice["notification"]["notification_id"]

    def test_three_receipt_types_and_reroute(self):
        # 已接手
        accepted = self.service.receive_receipt(self.notice_id, "accepted", 42, accepted_at=41)
        self.assertEqual(accepted["effect"], "recorded")
        # 无法到场 -> 按当前规则改派 family-6
        outcome = self.service.receive_receipt(self.notice_id, "cannot_attend", 44)
        self.assertEqual(outcome["effect"], "routed_or_pending")
        event = self.service.get_event(self.event_id)
        self.assertEqual(event["current_owner"], "family-6")
        rerouted = [n for n in self.service.contact_history() if n["contact"] == "family-6"][0]
        # 已撤联系人不会被补发：先撤回 family 再让其无法到场
        self.service.revoke_contact("family-6", 45)
        self.service.receive_receipt(rerouted["notification_id"], "cannot_attend", 46)
        event = self.service.get_event(self.event_id)
        self.assertIsNone(event["current_owner"])
        self.assertIn("无可用后续联系人", event["handover_note"])

    def test_verified_safe_closes_with_basis(self):
        self.service.receive_receipt(
            self.notice_id, "verified_safe", 50, evidence={"at_scene": True}
        )
        event = self.service.get_event(self.event_id)
        self.assertEqual(event["status"], EventStatus.VERIFIED_SAFE.value)
        self.assertEqual(event["close_basis"], "verified_safe_receipt")
        # 结束后再来回执只报错，不改状态
        with self.assertRaises(CareError) as raised:
            self.service.receive_receipt(self.notice_id, "accepted", 51)
        self.assertEqual(raised.exception.code, "event_closed")

    def test_late_accept_after_revoke_is_recorded_but_not_restored(self):
        # 47 撤回邻居；53 才到“46 已接手”的迟到回执
        self.service.revoke_contact("neighbor-2", 47)
        outcome = self.service.receive_receipt(self.notice_id, "accepted", 53, accepted_at=46)
        self.assertTrue(outcome["late"])
        event = self.service.get_event(self.event_id)
        self.assertNotEqual(event["current_owner"], "neighbor-2")
        self.assertEqual(event["care_state"], "late_accept_after_revoke")

    def test_late_receipt_decisions(self):
        self.service.revoke_contact("neighbor-2", 47)
        self.service.receive_receipt(self.notice_id, "accepted", 53, accepted_at=46)
        # 授权已不存在，维持原安排被拒绝
        with self.assertRaises(CareError) as raised:
            self.service.decide_late_receipt(self.event_id, "neighbor-2", "keep_original", 54)
        self.assertEqual(raised.exception.code, "authorization_gone")
        # 值守站接手核对，暂停补发
        event = self.service.decide_late_receipt(self.event_id, "neighbor-2", "station_takeover", 54)
        self.assertEqual(event["status"], EventStatus.STATION_VERIFYING.value)
        self.assertEqual(event["current_owner"], "desk")
        with self.assertRaises(CareError):  # 撤回联系人不能再派
            self.service.dispatch(self.event_id, "neighbor-2", 55, "visit_request")

    def test_keep_original_when_authorization_still_valid(self):
        # 不撤回，迟到的 accepted 到达后可维持原安排
        outcome = self.service.receive_receipt(self.notice_id, "accepted", 53, accepted_at=46)
        self.assertFalse(outcome["late"])
        event = self.service.decide_late_receipt(self.event_id, "neighbor-2", "keep_original", 54)
        self.assertEqual(event["current_owner"], "neighbor-2")


class CloseGuardTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.register_device("d-1", 0)
        self.event_id = self.service.record_device_state("d-1", 30, "offline")["event_id"]

    def test_cannot_close_without_clear_scope(self):
        with self.assertRaises(CareError) as raised:
            self.service.close_event(self.event_id, 40, "值守员判断没事", ["x"])
        self.assertEqual(raised.exception.code, "scope_unclear")
        with self.assertRaises(CareError) as raised:
            self.service.close_event(self.event_id, 40, "", [])
        self.assertEqual(raised.exception.code, "basis_required")

    def test_close_after_station_verification(self):
        self.service.station_verify(self.event_id, 41, "电话核实本人应答", ["call-log-1"])
        closed = self.service.close_event(
            self.event_id, 42, "station_verified: 电话核实本人应答", ["call-log-1"]
        )
        self.assertEqual(closed["status"], EventStatus.CLOSED.value)
        self.assertEqual(closed["close_refs"], ["call-log-1"])

    def test_resident_confirm_safe_is_valid_basis(self):
        self.service.resident_confirm_safe(self.event_id, 41)
        closed = self.service.close_event(self.event_id, 42, "verified_safe_receipt", ["resident"])
        self.assertEqual(closed["status"], EventStatus.CLOSED.value)


class HandoverTest(unittest.TestCase):
    def setUp(self):
        self.service = build_service()
        self.service.register_device("d-1", 0)
        self.event_id = self.service.record_device_state("d-1", 30, "offline")["event_id"]
        self.service.dispatch(self.event_id, "neighbor-2", 41, "visit_request", notification_id="c-19")

    def test_desk_handover_keeps_owner_action_and_care_state(self):
        snapshot = self.service.handover(self.event_id, "night-shift", 60, role="desk", note="等待回执")
        event = self.service.get_event(self.event_id)
        self.assertEqual(event["current_owner"], "night-shift")
        self.assertEqual(snapshot["current_owner"], "night-shift")
        self.assertEqual(snapshot["care_state"], "contact_dispatched")
        self.assertIn("c-19", snapshot["open_notifications"])
        # 快照不含完整住户台账，只交接必要信息
        self.assertNotIn("rules", snapshot)
        self.assertNotIn("windows", snapshot)
        event = self.service.get_event(self.event_id)
        self.assertEqual(event["current_owner"], "night-shift")

    def test_partner_handover_only_with_assigned_task(self):
        packet = self.service.handover(self.event_id, "neighbor-2", 61, role="partner")
        self.assertEqual(packet["task"]["notification_id"], "c-19")
        self.assertEqual(packet["packet"]["detail"], "visit_request")
        with self.assertRaises(CareError) as raised:
            self.service.handover(self.event_id, "family-6", 62, role="partner")
        self.assertEqual(raised.exception.code, "not_assigned")

    def test_followup_continues_after_handover(self):
        self.service.handover(self.event_id, "night-shift", 60, role="desk")
        self.service.receive_receipt("c-19", "cannot_attend", 61)
        event = self.service.get_event(self.event_id)
        self.assertEqual(event["current_owner"], "family-6")  # 同事件内继续办理


class SnapshotTest(unittest.TestCase):
    def test_snapshot_restore_roundtrip(self):
        service = build_service()
        service.register_device("d-1", 0)
        event_id = service.record_device_state("d-1", 30, "offline")["event_id"]
        service.dispatch(event_id, "neighbor-2", 41, "visit_request", notification_id="c-19")
        data = service.snapshot()

        rebuilt = CareService()
        rebuilt.restore(data)
        self.assertEqual(rebuilt.resident_ledger(), service.resident_ledger())
        self.assertEqual(rebuilt.list_events(), service.list_events())
        self.assertEqual(rebuilt.contact_history(), service.contact_history())
        # 序号继续递增不冲突
        notice = rebuilt.dispatch(
            rebuilt.list_events()[0]["event_id"], "family-6", 42, "call_request"
        )
        self.assertTrue(notice["notification"]["notification_id"].startswith("n-"))


if __name__ == "__main__":
    unittest.main()
