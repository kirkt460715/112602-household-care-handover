"""关怀服务的 HTTP 接口。

三类角色：
- desk（值守台/值守员）：派单、迟到回执处置、交班、核实、关闭、资料同步；
- resident（住户）：登记联络窗口与授权、撤回、查联系历史、本人确认平安；
- partner（合作联系点）：只看自己名下任务、回三种回执。

服务只监听回环地址。令牌通过构造参数或环境变量注入，不落盘。
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from .feed_client import FeedClient
from .model import CareError
from .service import CareService
from .storage import JsonStore

# CareError.code -> HTTP 状态
ERROR_STATUS = {
    "invalid_contact": 400,
    "invalid_channel": 400,
    "invalid_scope": 400,
    "invalid_detail": 400,
    "invalid_period": 400,
    "invalid_device_status": 400,
    "invalid_receipt": 400,
    "invalid_decision": 400,
    "invalid_role": 400,
    "invalid_status": 400,
    "basis_required": 400,
    "refs_required": 400,
    "contact_unknown": 404,
    "event_not_found": 404,
    "notification_not_found": 404,
    "device_not_found": 404,
    "no_late_receipt": 404,
    "device_exists": 409,
    "device_retired": 409,
    "notification_exists": 409,
    "event_closed": 409,
    "no_open_event": 409,
    "not_authorized": 403,
    "disclosure_forbidden": 403,
    "authorization_gone": 403,
    "not_assigned": 403,
    "window_unavailable": 409,
    "notification_active": 409,
    "no_active_authorization": 409,
    "scope_unclear": 422,
    "unknown_source": 422,
    "upstream_failed": 502,
}

ROLE_PREFIX = ("desk", "resident", "partner")


class CareServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        port: int,
        service: Optional[CareService] = None,
        tokens: Optional[dict[str, str]] = None,
        store: Optional[JsonStore] = None,
        feed_url: Optional[str] = None,
    ):
        self.service = service or CareService()
        self.store = store
        self.tokens = dict(tokens or {})
        self.feed_url = feed_url
        self.feed_cursor = 0
        if store is not None:
            store.load(self.service)
        super().__init__(("127.0.0.1", port), CareHandler)

    def save(self) -> None:
        if self.store is not None:
            self.store.save(self.service)

    def sync_feed(self) -> dict[str, Any]:
        if self.feed_url is None:
            raise CareError("upstream_failed", "未配置资料端地址")
        client = FeedClient(self.feed_url)
        if not self.service.resident_id:
            client.load_register_into(self.service, at=0)
        before = self.feed_cursor
        self.feed_cursor = client.drain(self.service, cursor=self.feed_cursor)
        return {"cursor_before": before, "cursor_after": self.feed_cursor}


class CareHandler(BaseHTTPRequestHandler):
    server: CareServer

    # ------------------------------------------------------------------
    # 基础收发
    # ------------------------------------------------------------------
    def respond(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            raise CareError("invalid_body", "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise CareError("invalid_body", "请求体必须是 JSON 对象")
        return data

    def authenticate(self) -> tuple[str, str]:
        token = self.headers.get("X-Auth-Token", "")
        principal = self.server.tokens.get(token)
        if principal is None:
            raise CareError("unauthorized", "缺少或无效的访问令牌")
        role, _, identity = principal.partition(":")
        return role, identity or role

    def require_role(self, allowed: tuple[str, ...]) -> tuple[str, str]:
        role, identity = self.authenticate()
        if role not in allowed:
            raise CareError("forbidden", "该角色无权访问此接口")
        return role, identity

    # ------------------------------------------------------------------
    # 路由
    # ------------------------------------------------------------------
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        url = urlsplit(self.path)
        parts = [part for part in url.path.split("/") if part]
        query = parse_qs(url.query)
        try:
            if parts == ["health"]:
                self.respond(200, {"status": "ok"})
                return
            if len(parts) < 2 or parts[0] != "api":
                raise CareError("not_found", "未知路径")
            role, identity = self.authenticate()
            resource = parts[1]
            resource_id = parts[2] if len(parts) > 2 else None
            if method == "GET":
                self._route_get(role, identity, resource, resource_id, query)
            else:
                body = self.read_json()
                self._route_post(role, identity, resource, resource_id, body)
            self.server.save()
        except CareError as exc:
            status = ERROR_STATUS.get(exc.code, 400)
            if exc.code == "unauthorized":
                status = 401
            if exc.code == "forbidden":
                status = 403
            if exc.code == "not_found":
                status = 404
            self.respond(status, {"error": exc.code, "message": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self.respond(400, {"error": "invalid_body", "message": str(exc)})

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def _route_get(self, role: str, identity: str, resource: str, resource_id: Optional[str], query: dict) -> None:
        service = self.server.service
        if resource == "events" and resource_id:
            self.require_role(("desk",))
            self.respond(200, service.get_event(resource_id))
            return
        if resource == "events":
            self.require_role(("desk", "partner"))
            if role == "partner":
                self.respond(200, {"items": service.tasks_for(identity)})
                return
            status_filter = query.get("status", [None])[0]
            self.respond(200, {"items": service.list_events(status_filter)})
            return
        if resource == "ledger":
            self.require_role(("resident", "desk"))
            self.respond(200, service.resident_ledger())
            return
        if resource == "history":
            self.require_role(("resident",))
            self.respond(200, {"items": service.contact_history()})
            return
        if resource == "tasks":
            self.require_role(("partner",))
            self.respond(200, {"items": service.tasks_for(identity)})
            return
        raise CareError("not_found", "未知路径")

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def _route_post(self, role: str, identity: str, resource: str, resource_id: Optional[str], body: dict) -> None:
        service = self.server.service

        if resource == "register" and role == "resident":
            result = service.register_resident(
                body["resident"],
                body.get("windows", []),
                body.get("rules", []),
                int(body["at"]),
            )
            self.respond(201, result)
            return
        if resource == "windows" and role == "resident":
            self.respond(201, service.add_window(body, int(body["at"])))
            return
        if resource == "authorizations" and role == "resident":
            self.respond(201, service.grant_rule(body, int(body["at"])))
            return
        if resource == "revocations" and role == "resident":
            self.respond(200, service.revoke_contact(body["contact"], int(body["at"])))
            return
        if resource == "confirm-safe" and role == "resident":
            self.respond(
                200,
                service.resident_confirm_safe(body["event_id"], int(body["at"]), body.get("basis", "resident_self_confirm")),
            )
            return

        if resource == "devices":
            self.require_role(("desk",))
            if body.get("action") == "register":
                self.respond(201, service.register_device(body["device_id"], int(body["at"]), body.get("valid_to")))
                return
            if body.get("action") == "pause":
                self.respond(200, service.pause_device(body["device_id"], int(body["at"])))
                return
            if body.get("action") == "replace":
                self.respond(
                    200,
                    service.replace_device(body["old_id"], body["new_id"], int(body["at"])),
                )
                return
            if body.get("action") == "state":
                self.respond(
                    200,
                    service.record_device_state(
                        body["device_id"],
                        int(body["at"]),
                        body["status"],
                        body.get("last_seen"),
                    ),
                )
                return
            raise CareError("invalid_body", "未知设备动作")

        if resource == "dispatch" and role == "desk":
            self.respond(
                201,
                service.dispatch(
                    body["event_id"],
                    body["contact"],
                    int(body["at"]),
                    body["detail"],
                    notification_id=body.get("request"),
                ),
            )
            return
        if resource == "late-decisions" and role == "desk":
            self.respond(
                200,
                service.decide_late_receipt(
                    body["event_id"], body["contact"], body["decision"], int(body["at"])
                ),
            )
            return
        if resource == "handovers":
            if role == "partner":
                target_role = "partner"
            elif role == "desk":
                self.require_role(("desk",))
                target_role = body.get("role", "desk")
            else:
                raise CareError("forbidden", "该角色无权交班")
            self.respond(
                200,
                service.handover(
                    body["event_id"],
                    body["to"],
                    int(body["at"]),
                    target_role,
                    body.get("note"),
                ),
            )
            return
        if resource == "verify" and role == "desk":
            self.respond(
                200,
                service.station_verify(body["event_id"], int(body["at"]), body["basis"], body.get("refs")),
            )
            return
        if resource == "close" and role == "desk":
            self.respond(
                200,
                service.close_event(
                    body["event_id"], int(body["at"]), body["basis"], body.get("refs", [])
                ),
            )
            return
        if resource == "feed-sync" and role == "desk":
            self.respond(200, self.server.sync_feed())
            return

        if resource == "receipts" and role == "partner":
            self.respond(
                200,
                service.receive_receipt(
                    body["notification_id"],
                    body["result"],
                    int(body["at"]),
                    accepted_at=body.get("accepted_at"),
                    identity=identity,
                    evidence=body.get("evidence"),
                ),
            )
            return

        raise CareError("forbidden", "该角色无权访问此接口或接口不存在")

    def log_message(self, *args: Any) -> None:
        pass


def build_default_tokens() -> dict[str, str]:
    """从环境变量读取令牌；未设置时给出仅供本地联调的默认值。"""
    return {
        os.environ.get("DESK_TOKEN", "desk-local-token"): "desk:district-desk",
        os.environ.get("RESIDENT_TOKEN", "resident-local-token"): "resident:care-3",
        os.environ.get("PARTNER_TOKEN", "partner-local-token"): "partner:neighbor-2",
    }
