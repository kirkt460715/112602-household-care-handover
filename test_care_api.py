"""HTTP 接口测试：角色令牌、路由权限与完整办理链路。"""

from http.client import HTTPConnection
import json
from threading import Thread
import unittest

from partner_feed import PartnerFeed

from care import CareServer
from care.api import build_default_tokens


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.feed = PartnerFeed(0)
        self.addCleanup(self.feed.server_close)
        thread = Thread(target=self.feed.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.feed.shutdown)

        self.tokens = build_default_tokens()
        self.server = CareServer(
            0,
            tokens=self.tokens,
            feed_url=f"http://127.0.0.1:{self.feed.server_port}",
        )
        self.addCleanup(self.server.server_close)
        api_thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        api_thread.start()
        self.addCleanup(api_thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_port

    def call(self, method, path, token=None, body=None):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["X-Auth-Token"] = token
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = json.loads(response.read())
            return response.status, data
        finally:
            connection.close()

    def test_health_and_auth(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.assertEqual(self.call("GET", "/api/events")[0], 401)
        status, body = self.call("GET", "/api/events", token="wrong")
        self.assertEqual((status, body["error"]), (401, "unauthorized"))

    def test_role_boundaries(self):
        desk = "desk-local-token"
        resident = "resident-local-token"
        partner = "partner-local-token"
        # 合作点不能派单，住户不能关闭事件，值守台不能查住户联系历史
        self.assertEqual(self.call("POST", "/api/dispatch", partner, {"event_id": "x"})[0], 403)
        self.assertEqual(
            self.call("POST", "/api/close", resident, {"event_id": "x", "at": 1, "basis": "b", "refs": ["r"]})[0],
            403,
        )
        self.assertEqual(self.call("GET", "/api/history", desk)[0], 403)

    def test_full_flow_over_http(self):
        desk = "desk-local-token"
        resident = "resident-local-token"
        partner = "partner-local-token"

        # 一次同步资料端：台账 + 全部到达事实
        status, sync = self.call("POST", "/api/feed-sync", desk, {})
        self.assertEqual(status, 200)
        self.assertEqual(sync["cursor_after"], 6)

        status, events = self.call("GET", "/api/events", desk)
        self.assertEqual(status, 200)
        self.assertEqual(len(events["items"]), 1)
        event_id = events["items"][0]["event_id"]

        # 住户查联系历史：邻居知会已失效，family-6 在途
        status, history = self.call("GET", "/api/history", resident)
        self.assertEqual(status, 200)
        contacts = [item["contact"] for item in history["items"]]
        self.assertEqual(contacts, ["neighbor-2", "family-6"])

        # 合作点只看自己的任务；partner 令牌身份是 neighbor-2，任务已失效 -> 空
        status, tasks = self.call("GET", "/api/tasks", partner)
        self.assertEqual((status, tasks["items"]), (200, []))

        # 迟到回执判定 + 值守核实 + 关闭守卫
        status, body = self.call(
            "POST",
            "/api/late-decisions",
            desk,
            {"event_id": event_id, "contact": "neighbor-2", "decision": "station_takeover", "at": 60},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["current_owner"], "desk")

        # 完成范围不明确不能关闭
        status, body = self.call(
            "POST",
            "/api/close",
            desk,
            {"event_id": event_id, "at": 61, "basis": "先关了再说", "refs": []},
        )
        self.assertEqual((status, body["error"]), (400, "refs_required"))

        status, _ = self.call(
            "POST",
            "/api/verify",
            desk,
            {"event_id": event_id, "at": 62, "basis": "电话核实本人应答", "refs": ["call-log-1"]},
        )
        self.assertEqual(status, 200)
        status, closed = self.call(
            "POST",
            "/api/close",
            desk,
            {
                "event_id": event_id,
                "at": 63,
                "basis": "station_verified: 电话核实本人应答",
                "refs": ["call-log-1"],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(closed["status"], "closed")

    def test_resident_authorization_change_takes_effect_for_future(self):
        desk = "desk-local-token"
        resident = "resident-local-token"
        # 登记新设备并产生离线事件
        status, _ = self.call(
            "POST", "/api/devices", desk, {"action": "register", "device_id": "d-9", "at": 0}
        )
        self.assertEqual(status, 201)
        status, state = self.call(
            "POST",
            "/api/devices",
            desk,
            {"action": "state", "device_id": "d-9", "at": 10, "status": "offline"},
        )
        event_id = state["event_id"]

        # 住户登记窗口与授权
        self.call(
            "POST",
            "/api/windows",
            resident,
            {"contact": "neighbor-2", "channel": "neighbor", "valid_from": 0, "at": 0},
        )
        self.call(
            "POST",
            "/api/authorizations",
            resident,
            {"contact": "neighbor-2", "scope": "unconfirmed_check", "detail": "visit_request", "at": 0},
        )
        # 超范围透露被拒
        status, body = self.call(
            "POST",
            "/api/dispatch",
            desk,
            {"event_id": event_id, "contact": "neighbor-2", "at": 11, "detail": "device_detail"},
        )
        self.assertEqual((status, body["error"]), (403, "disclosure_forbidden"))
        # 住户提升授权后，同样请求被允许（只影响后续动作）
        self.call(
            "POST",
            "/api/authorizations",
            resident,
            {"contact": "neighbor-2", "scope": "unconfirmed_check", "detail": "device_detail", "at": 12},
        )
        status, dispatched = self.call(
            "POST",
            "/api/dispatch",
            desk,
            {"event_id": event_id, "contact": "neighbor-2", "at": 13, "detail": "device_detail"},
        )
        self.assertEqual(status, 201)
        self.assertIn("device_ref", dispatched["packet"])

    def test_partner_handover_does_not_receive_full_ledger(self):
        desk, _, partner = "desk-local-token", "resident-local-token", "partner-local-token"
        self.call("POST", "/api/feed-sync", desk, {})
        event_id = self.call("GET", "/api/events", desk)[1]["items"][0]["event_id"]
        # neighbor-2 的知会已失效，合作点交班应被拒绝
        status, body = self.call(
            "POST",
            "/api/handovers",
            partner,
            {"event_id": event_id, "to": "neighbor-2", "at": 70},
        )
        self.assertEqual((status, body["error"]), (403, "not_assigned"))
        # 值守台内部交班只回必要快照
        status, snapshot = self.call(
            "POST",
            "/api/handovers",
            desk,
            {"event_id": event_id, "to": "night-shift", "at": 70, "role": "desk", "note": "继续跟进 family-6"},
        )
        self.assertEqual(status, 200)
        self.assertNotIn("rules", snapshot)
        self.assertNotIn("windows", snapshot)
        self.assertEqual(snapshot["care_state"], "rerouted")


if __name__ == "__main__":
    unittest.main()
