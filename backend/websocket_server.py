from __future__ import annotations

import asyncio
import json
from typing import Set

import websockets


class WebSocketBridge:
    """Local websocket server for extension broadcast."""

    def __init__(self, host: str = "localhost", port: int = 8765) -> None:
        self.host = host
        self.port = port
        self.clients: Set[websockets.WebSocketServerProtocol] = set()
        self._server: websockets.server.Serve | None = None

    async def _handler(self, ws):
        self.clients.add(ws)
        try:
            async for _msg in ws:
                pass
        finally:
            self.clients.discard(ws)

    async def start(self) -> None:
        self._server = await websockets.serve(self._handler, self.host, self.port)

    async def broadcast(self, payload: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps(payload, ensure_ascii=False)
        await asyncio.gather(*(client.send(msg) for client in list(self.clients)), return_exceptions=True)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
