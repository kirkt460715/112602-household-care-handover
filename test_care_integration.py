"""端到端：通过本地资料端 partner_feed 消费样例故事。

样例时序：
1. d-1 离线（未确认事件，不等于需要帮助）
2. 值守台向 neighbor-2 派单 c-19（仅 visit_request）
3. 住户撤回 neighbor-2
4. 新设备 d-2 报到上线（换新，时段重叠）
5. neighbor-2 的 accepted 回执晚到（accepted_at=46，撤回发生在 47 之后到达 53）
6. cannot_attend 回执到达
"""

from threading import Thread
import unittest

from partner_feed import PartnerFeed

from care import CareService, EventStatus, FeedClient, NotificationStatus


class FeedIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.feed = PartnerFeed(0)
        self.addCleanup(self.feed.server_close)
        thread = Thread(
            target=self.feed.serve_forever,
            kwargs={"poll_interval": 0.02},
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.feed.shutdown)
        self.client = FeedClient(f"http://127.0.0.1:{self.feed.server_port}", retries=2)
        self.service = CareService()

    def drain_all(self):
        cursor = 0
        for _ in range(10):
            new_cursor = self.client.drain(self.service, cursor=cursor)
            if new_cursor == cursor:
                break
            cursor = new_cursor
        return cursor

    def test_full_sample_story(self):
        self.client.load_register_into(self.service, at=0)
        ledger = self.service.resident_ledger()
        self.assertEqual(ledger["resident"], "care-3")
        # 台账中 neighbor-2 的窗口与规则在 47 收口
        neighbor_window = next(w for w in ledger["windows"] if w["contact"] == "neighbor-2")
        self.assertEqual(neighbor_window["valid_to"], 47)
        neighbor_rule = next(r for r in ledger["rules"] if r["contact"] == "neighbor-2")
        self.assertEqual(neighbor_rule["valid_to"], 47)
        # 设备时段可重叠
        devices = {d["device_id"]: d for d in ledger["devices"]}
        self.assertEqual((devices["d-1"]["valid_from"], devices["d-1"]["valid_to"]), (0, 50))
        self.assertEqual(devices["d-2"]["valid_from"], 48)

        cursor = self.drain_all()
        self.assertEqual(cursor, 6)

        events = self.service.list_events()
        # 只有 d-1 的离线产生了一个事件；d-2 在线不产生待办
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["device_id"], "d-1")
        self.assertEqual(event["status"], EventStatus.ACTIVE.value)
        # 离线始终是未确认开局
        self.assertEqual(event["history"][0]["action"], "opened_unconfirmed")

        notices = self.service.contact_history()
        self.assertEqual([n["contact"] for n in notices], ["neighbor-2", "family-6"])
        by_contact = {n["contact"]: n for n in notices}
        # c-19 保留实际去向：派单 -> 撤回失效 -> 迟到 accepted -> cannot_attend
        c19 = by_contact["neighbor-2"]
        self.assertEqual(c19["notification_id"], "c-19")
        self.assertEqual(c19["status"], NotificationStatus.SUPERSEDED.value)
        self.assertEqual(c19["disclosed_detail"], "visit_request")  # 设备详情从未给邻居
        results = [r["result"] for r in c19["receipts"]]
        self.assertEqual(results, ["accepted", "cannot_attend"])
        # 延迟回执的独立证据经重试取回
        self.assertEqual(c19["receipts"][0]["evidence"], {"accepted_at": 46, "at_scene": False})
        self.assertTrue(c19["receipts"][0]["at"] > c19["receipts"][0]["accepted_at"])

        # 无法到场后按当前规则改派仍有效授权的 family-6（邻居已撤回，不会被补发）
        rerouted = by_contact["family-6"]
        self.assertEqual(rerouted["status"], NotificationStatus.DISPATCHED.value)
        self.assertEqual(event["current_owner"], "family-6")
        self.assertEqual(event["care_state"], "rerouted")

        # 撤回后迟到 accepted 已记录在 c-19 上，可被值守台事后判定
        self.assertTrue(
            any(entry["action"] == "late_accept_pending_decision" for entry in event["history"])
        )

        # 新设备证据已独立留痕
        self.assertIn("d-2", self.service.device_evidence)

    def test_late_receipt_decision_and_close_after_drain(self):
        self.client.load_register_into(self.service, at=0)
        self.drain_all()
        event_id = self.service.list_events()[0]["event_id"]

        # 值守台判定：由值守站继续核对，暂停向已撤联系人补发
        event = self.service.decide_late_receipt(event_id, "neighbor-2", "station_takeover", 60)
        self.assertEqual(event["status"], EventStatus.STATION_VERIFYING.value)
        self.assertEqual(event["current_owner"], "desk")

        # 核实平安后才能关闭，且必须附依据
        self.service.station_verify(event_id, 61, "电话核实本人应答", ["call-log-1"])
        closed = self.service.close_event(
            event_id, 62, "station_verified: 电话核实本人应答", ["call-log-1"]
        )
        self.assertEqual(closed["status"], EventStatus.CLOSED.value)

    def test_drain_is_idempotent_by_cursor(self):
        self.client.load_register_into(self.service, at=0)
        first = self.drain_all()
        events_after_first = len(self.service.list_events())
        again = self.drain_all()
        self.assertEqual(again, first)
        self.assertEqual(len(self.service.list_events()), events_after_first)


if __name__ == "__main__":
    unittest.main()
