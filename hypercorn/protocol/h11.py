from itertools import chain
from typing import Awaitable, Callable, Optional, Tuple, Type, Union

import h11

from .events import (
    Body,
    Data,
    EndBody,
    EndData,
    Event as StreamEvent,
    Request,
    Response,
    StreamClosed,
)
from .http_stream import HTTPStream
from .ws_stream import WSStream
from ..config import Config
from ..events import Closed, Event, RawData, Updated
from ..typing import Event as IOEvent, H11SendableEvent
from ..utils import response_headers

STREAM_ID = 1


class H11WSConnection:
    # This class matches the h11 interface, and either passes data
    # through without altering it (for Data, EndData) or sends h11
    # events (Response, Body, EndBody).
    our_state = None  # Prevents recycling the connection
    they_are_waiting_for_100_continue = False

    def __init__(self, h11_connection: h11.Connection) -> None:
        self.buffer = bytearray()
        self.h11_connection = h11_connection

    def receive_data(self, data: bytes) -> None:
        self.buffer.extend(data)

    def next_event(self) -> Data:
        if self.buffer:
            event = Data(stream_id=STREAM_ID, data=bytes(self.buffer))
            self.buffer = bytearray()
            return event
        else:
            return h11.NEED_DATA

    def send(self, event: H11SendableEvent) -> bytes:
        return self.h11_connection.send(event)


class H11Protocol:
    def __init__(
        self,
        config: Config,
        ssl: bool,
        client: Optional[Tuple[str, int]],
        server: Optional[Tuple[str, int]],
        send: Callable[[Event], Awaitable[None]],
        spawn_app: Callable[[dict, Callable], Awaitable[Callable]],
        event_class: Type[IOEvent],
    ) -> None:
        self.can_read = event_class()
        self.client = client
        self.config = config
        self.connection = h11.Connection(
            h11.SERVER, max_incomplete_event_size=self.config.h11_max_incomplete_size
        )
        self.send = send
        self.server = server
        self.spawn_app = spawn_app
        self.ssl = ssl
        self.stream: Optional[Union[HTTPStream, WSStream]] = None

    @property
    def idle(self) -> bool:
        return self.stream is None

    async def initiate(self) -> None:
        pass

    async def handle(self, event: Event) -> None:
        if isinstance(event, RawData):
            self.connection.receive_data(event.data)
            await self._handle_events()
        elif isinstance(event, Closed):
            if self.stream is not None:
                await self._close_stream()

    async def stream_send(self, event: StreamEvent) -> None:
        if isinstance(event, Response):
            if event.status_code >= 200:
                await self._send_h11_event(
                    h11.Response(
                        headers=chain(event.headers, response_headers("h11")),
                        status_code=event.status_code,
                    )
                )
            else:
                await self._send_h11_event(
                    h11.InformationalResponse(
                        headers=chain(event.headers, response_headers("h11")),
                        status_code=event.status_code,
                    )
                )
        elif isinstance(event, Body):
            await self._send_h11_event(h11.Data(data=event.data))
        elif isinstance(event, EndBody):
            await self._send_h11_event(h11.EndOfMessage())
            await self._maybe_recycle()
        elif isinstance(event, Data):
            await self.send(RawData(data=event.data))
        elif isinstance(event, (EndData, StreamClosed)):
            await self._maybe_recycle()

    async def _handle_events(self) -> None:
        while True:
            if self.connection.they_are_waiting_for_100_continue:
                await self._send_h11_event(
                    h11.InformationalResponse(status_code=100, headers=response_headers("h11"))
                )
            try:
                event = self.connection.next_event()
            except h11.RemoteProtocolError:
                await self._send_error_response(400)
                await self.send(Closed())
                break
            else:
                if isinstance(event, h11.Request):
                    # self._raise_if_upgrade(event)
                    await self._create_stream(event)
                elif isinstance(event, h11.Data):
                    await self.stream.handle(Body(stream_id=STREAM_ID, data=event.data))
                elif isinstance(event, h11.EndOfMessage):
                    await self.stream.handle(EndBody(stream_id=STREAM_ID))
                elif isinstance(event, Data):
                    # WebSocket pass through
                    await self.stream.handle(event)
                elif event is h11.PAUSED:
                    await self.can_read.clear()
                    await self.can_read.wait()
                    break
                elif isinstance(event, h11.ConnectionClosed) or event is h11.NEED_DATA:
                    break

    async def _create_stream(self, request: h11.Request) -> None:
        upgrade_value = ""
        connection_value = ""
        for name, value in request.headers:
            sanitised_name = name.decode().strip().lower()
            if sanitised_name == "upgrade":
                upgrade_value = value.decode().strip()
            elif sanitised_name == "connection":
                connection_value = value.decode().strip()

        connection_tokens = connection_value.lower().split(",")
        if (
            any(token.strip() == "upgrade" for token in connection_tokens)
            and upgrade_value.lower() == "websocket"
            and request.method.decode().upper() == "GET"
        ):
            self.stream = WSStream(
                self.config,
                self.ssl,
                self.client,
                self.server,
                self.stream_send,
                self.spawn_app,
                STREAM_ID,
            )
            self.connection = H11WSConnection(self.connection)
        else:
            self.stream = HTTPStream(
                self.config,
                self.ssl,
                self.client,
                self.server,
                self.stream_send,
                self.spawn_app,
                STREAM_ID,
            )
        await self.stream.handle(
            Request(
                stream_id=STREAM_ID,
                headers=request.headers,
                http_version=request.http_version.decode(),
                method=request.method.decode().upper(),
                raw_path=request.target,
            )
        )

    async def _send_h11_event(self, event: H11SendableEvent) -> None:
        data = self.connection.send(event)
        await self.send(RawData(data=data))

    async def _send_error_response(self, status_code: int) -> None:
        await self._send_h11_event(
            h11.Response(
                status_code=status_code,
                headers=chain(
                    [(b"content-length", b"0"), (b"connection", b"close")], response_headers("h11")
                ),
            )
        )
        await self._send_h11_event(h11.EndOfMessage())

    async def _maybe_recycle(self) -> None:
        await self._close_stream()
        if self.connection.our_state is h11.DONE:
            try:
                self.connection.start_next_cycle()
            except h11.LocalProtocolError:
                await self.send(Closed())
            else:
                self.response = None
                self.scope = None
                await self.can_read.set()
                await self._handle_events()
                await self.send(Updated())
        else:
            await self.can_read.set()
            await self.send(Closed())

    async def _close_stream(self) -> None:
        if self.stream is not None:
            await self.stream.handle(StreamClosed(stream_id=STREAM_ID))
            self.stream = None