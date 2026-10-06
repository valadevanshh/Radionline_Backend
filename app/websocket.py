import logging
from typing import Dict, Iterable, List, Optional
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        # socket -> logged-in user id (from the JWT "sub"), for per-user notifications
        self.connection_users: Dict[WebSocket, str] = {}

    async def connect(self, websocket: WebSocket, user_id: Optional[str] = None):
        await websocket.accept()
        self.active_connections.append(websocket)
        if user_id is not None:
            self.connection_users[websocket] = str(user_id)
        logger.info(f"WebSocket client connected. Total connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.connection_users.pop(websocket, None)
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info(f"WebSocket client disconnected. Total connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        logger.info(f"Broadcasting WebSocket message: {message.get('type')}")
        disconnected_clients = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
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
