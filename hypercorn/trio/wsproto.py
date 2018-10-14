from typing import Optional, Type, Union

import h11
import trio
import wsproto

from .base import HTTPServer
from ..common.wsproto import (
    AcceptConnection, CloseConnection, Data, FrameTooLarge, WebsocketBuffer, WebsocketMixin,
    WsprotoEvent,
)
from ..config import Config
from ..typing import ASGIFramework, H11SendableEvent
from ..utils import WebsocketState

MAX_RECV = 2 ** 16


class MustCloseError(Exception):
    pass


class WebsocketServer(HTTPServer, WebsocketMixin):

    def __init__(
            self,
            app: Type[ASGIFramework],
            config: Config,
            stream: trio.abc.Stream,
            *,
            upgrade_request: Optional[h11.Request]=None,
    ) -> None:
        super().__init__(stream, 'wsproto')
        self.app = app
        self.config = config
        self.connection = wsproto.connection.WSConnection(
            wsproto.connection.SERVER, extensions=[wsproto.extensions.PerMessageDeflate()],
        )
        self.app_queue = trio.Queue(10)
        self.response: Optional[dict] = None
        self.scope: Optional[dict] = None
        self.state = WebsocketState.HANDSHAKE

        self.buffer = WebsocketBuffer(self.config.websocket_max_message_size)

        if upgrade_request is not None:
            fake_client = h11.Connection(h11.CLIENT)
            self.connection.receive_bytes(fake_client.send(upgrade_request))

    async def handle_connection(self) -> None:
        try:
            request = await self.read_request()
            async with trio.open_nursery() as nursery:
                nursery.start_soon(self.read_messages)
                await self.handle_websocket(request)
                if self.state == WebsocketState.HTTPCLOSED:
                    raise MustCloseError()
        except (trio.BrokenResourceError, trio.ClosedResourceError):
            self.app_queue.put_nowait({'type': 'websocket.disconnect'})
        except MustCloseError:
            await self.stream.send_all(self.connection.bytes_to_send())
        finally:
            await self.aclose()

    async def read_request(self) -> wsproto.events.ConnectionRequested:
        for event in self.connection.events():
            if isinstance(event, wsproto.events.ConnectionRequested):
                return event

    async def read_messages(self) -> None:
        while True:
            data = await self.stream.receive_some(MAX_RECV)
            self.connection.receive_bytes(data)
            for event in self.connection.events():
                if isinstance(event, wsproto.events.DataReceived):
                    try:
                        self.buffer.extend(event)
                    except FrameTooLarge:
                        self.connection.close(1009)  # CLOSE_TOO_LARGE
                        self.app_queue.put_nowait({'type': 'websocket.disconnect'})
                        raise MustCloseError()

                    if event.message_finished:
                        self.app_queue.put_nowait(self.buffer.to_message())
                        self.buffer.clear()
                elif isinstance(event, wsproto.events.ConnectionClosed):
                    self.app_queue.put_nowait({'type': 'websocket.disconnect'})
                    raise MustCloseError()

    async def asend(self, event: Union[H11SendableEvent, WsprotoEvent]) -> None:
        if isinstance(event, AcceptConnection):
            self.connection.accept(event.request)
            data = self.connection.bytes_to_send()
        elif isinstance(event, Data):
            self.connection.send_data(event.data)
            data = self.connection.bytes_to_send()
        elif isinstance(event, CloseConnection):
            self.connection.close(event.code)
            data = self.connection.bytes_to_send()
        else:
            data = self.connection._upgrade_connection.send(event)
        await self.stream.send_all(data)

    @property
    def scheme(self) -> str:
        return 'wss' if self._is_ssl else 'ws'
