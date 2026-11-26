"""对接本地资料端 partner_feed：拉取台账、到达事实与回执证据。

资料端的 /receipt 首次查询可能返回 503（delayed 样例），
本客户端只做重试与翻译，不自行办理任何业务动作。
"""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .model import CareError
from .service import CareService


class FeedClient:
    def __init__(self, base_url: str, timeout: float = 3.0, retries: int = 2):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries

    def _get(self, path: str) -> tuple[int, dict]:
        url = f"{self.base_url}{path}"
        request = Request(url, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {"error": "upstream_error"}
            return exc.code, body

    def fetch_register(self) -> dict:
        status, body = self._get("/register")
        if status != 200:
            raise CareError("upstream_failed", f"台账读取失败: {status}")
        return body

    def fetch_intake(self, since: int = 0, limit: int = 50) -> dict:
        query = urlencode({"since": since, "limit": limit})
        status, body = self._get(f"/intake?{query}")
        if status != 200:
            raise CareError("upstream_failed", f"到达记录读取失败: {status}")
        return body

    def fetch_receipt(self, action: str, desk: str, ref: str) -> dict | None:
        """查询回执证据；503 时按重试次数再试，仍不可查返回 None。"""
        query = urlencode({"action": action, "desk": desk, "ref": ref})
        for attempt in range(self.retries + 1):
            status, body = self._get(f"/receipt?{query}")
            if status == 200:
                return body.get("evidence")
            if status == 503 and attempt < self.retries:
                continue
            if status == 404:
                return None
            raise CareError("upstream_failed", f"回执查询失败: {status}")
        return None

    def load_register_into(self, service: CareService, at: int) -> None:
        """把资料端台账翻译为住户登记与设备登记。"""
        register = self.fetch_register()
        windows = [
            {
                "contact": rule["contact"],
                "channel": "neighbor" if "neighbor" in rule["contact"] else "family",
                "valid_from": 0,
                "valid_to": rule.get("until"),
            }
            for rule in register.get("contact_rules", [])
        ]
        rules = [
            {
                "contact": rule["contact"],
                "scope": rule["scope"],
                "detail": rule["detail"],
                "valid_from": 0,
                "valid_to": rule.get("until"),
            }
            for rule in register.get("contact_rules", [])
        ]
        service.register_resident(register["resident"], windows, rules, at)
        for device_id, info in register.get("equipment", {}).items():
            start, end = info["period"]
            service.register_device(device_id, start, valid_to=end)

    def drain(self, service: CareService, cursor: int = 0, limit: int = 50) -> int:
        """顺序消费到达记录，并为合作点与设备事实补查独立证据。

        返回新的游标，可重复调用直到没有新记录。
        """
        page = self.fetch_intake(since=cursor, limit=limit)
        for item in page.get("items", []):
            facts = item["facts"]
            source = item["source"]
            evidence = None
            if source == "partner":
                evidence = self.fetch_receipt("care-ack", "district-desk", facts["request"])
                if evidence is not None:
                    facts = dict(facts)
                    facts["evidence"] = evidence
            elif source == "device" and facts.get("status") == "online":
                device_evidence = self.fetch_receipt("device-state", "equipment", facts["id"])
                if device_evidence is not None:
                    service.attach_device_evidence(facts["id"], device_evidence, item["at"])
            service.ingest_fact(item["sequence"], item["at"], source, facts)
            cursor = item["sequence"]
        return page.get("cursor", cursor)
