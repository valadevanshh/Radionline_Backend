import logging
from typing import Dict, Iterable, List, Optional
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        # socket -> logged-in user id (from the JWT "sub"), for per-user notifications
        self.connection_users: Dict[WebSocket, str] = {}
        # socket -> user details (role, centerIds, doctorKeys) for scoped broadcasts
        self.connection_details: Dict[WebSocket, dict] = {}

    async def connect(
        self,
        websocket: WebSocket,
        user_id: Optional[str] = None,
        user_details: Optional[dict] = None,
    ):
        await websocket.accept()
        self.active_connections.append(websocket)
        if user_id is not None:
            self.connection_users[websocket] = str(user_id)
        if user_details is not None:
            self.connection_details[websocket] = user_details
        logger.info(f"WebSocket client connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.connection_users.pop(websocket, None)
        self.connection_details.pop(websocket, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        msg_type = message.get("type")
        logger.info(f"Broadcasting WebSocket message: {msg_type}")
        report_data = message.get("report") if isinstance(message.get("report"), dict) else {}
        center_id = report_data.get("radiologyCenterId")
        claimed_doc_id = str(message.get("claimedByDoctorId") or report_data.get("claimedByDoctorId") or "")
        assigned_doc_ids = report_data.get("assignedDoctorIds") or ["ALL"]

        disconnected_clients = []
        for connection in list(self.active_connections):
            details = self.connection_details.get(connection) or {}
            role = details.get("role")

            msg_to_send = message

            # Role-scoped data isolation:
            # If any doctor accepts the upload, other doctors and other centers must not see that data
            if role in ("SUPER_ADMIN", "MANAGER"):
                # Staff see all messages
                pass
            elif role == "CENTER":
                # Centers only receive events for their own center
                if center_id:
                    user_centers = details.get("centerIds") or set()
                    if str(center_id) not in user_centers:
                        continue
            elif role == "DOCTOR":
                doc_keys = details.get("doctorKeys") or set()
                if msg_type == "REPORT_CLAIMED":
                    if claimed_doc_id and claimed_doc_id in doc_keys:
                        msg_to_send = message
                    else:
                        # Other doctors receive a sanitized signal to remove the study from their queue
                        # without leaking patient details or scan data
                        msg_to_send = {
                            "type": "REPORT_CLAIMED",
                            "reportId": message.get("reportId") or report_data.get("id"),
                            "claimedByDoctorId": claimed_doc_id,
                            "claimedByDoctorName": message.get("claimedByDoctorName") or report_data.get("claimedByDoctorName"),
                        }
                elif msg_type == "REPORT_COMPLETED":
                    # Only the doctor who claimed/signed the study receives completion data
                    if claimed_doc_id and claimed_doc_id not in doc_keys:
                        continue
                elif msg_type == "NEW_REPORT":
                    # Only doctors assigned to this study (or ALL) receive new study notifications
                    if "ALL" not in assigned_doc_ids and not any(str(k) in assigned_doc_ids for k in doc_keys):
                        continue

            try:
                await connection.send_json(msg_to_send)
            except Exception as e:
                logger.warning(f"Error sending message to WebSocket client: {e}")
                disconnected_clients.append(connection)

        for client in disconnected_clients:
            self.disconnect(client)

    async def send_to_users(self, user_ids: Iterable, message: dict):
        """Send only to the sockets of the given user ids (never broadcast)."""
        targets = {str(u) for u in user_ids if u is not None}
        if not targets:
            return
        disconnected_clients = []
        for connection in list(self.active_connections):
            if self.connection_users.get(connection) not in targets:
                continue
            try:
                await connection.send_json(message)
            except Exception as e:
                logger.warning(f"Error sending message to WebSocket client: {e}")
                disconnected_clients.append(connection)

        for client in disconnected_clients:
            self.disconnect(client)

manager = ConnectionManager()

