import asyncio
import datetime
import logging
import typing
from abc import ABCMeta, abstractmethod
from asyncio import (FIRST_COMPLETED, AbstractEventLoop, BaseProtocol,
                     BaseTransport, CancelledError, Event, Future, Queue,
                     ReadTransport)
from typing import Callable, ClassVar, Optional, Tuple

from smartmeterdecode import hdlc

_LOGGER = logging.getLogger(__name__)


class SmartMeterProtocol(ReadTransport):
    connections: ClassVar[int] = 0

    def __init__(self, queue: Queue, frame_reader: hdlc.HdlcFrameReader = None) -> None:
        super().__init__()
        self.queue: Queue = queue
        self._frame_reader = (
            frame_reader
            if frame_reader
            else hdlc.HdlcFrameReader(use_octet_stuffing=False, use_abort_sequence=True)
        )
        self.done: Future = Future()
        self._transport: Optional[BaseTransport] = None
        self._transport_info: Optional[str] = None
        self.id: int = SmartMeterProtocol.connections
        SmartMeterProtocol.connections += 1

    def _set_transport_info(self) -> None:
        if self._transport:
            if hasattr(self._transport, "serial"):  # type: ignore
                self._transport_info = str(self._transport.serial)  # type: ignore
            else:
                peer_name = self._transport.get_extra_info("peername")
                if peer_name:
                    host, port = peer_name
                    self._transport_info = f"host {host} and port {port}"

            if not self._transport_info:
                self._transport_info = str(self._transport)

    def connection_made(self, transport: BaseTransport) -> None:
        """Called when a connection is made.

        The argument is the transport representing the connection.
        To receive data, wait for data_received() calls.
        When the connection is closed, connection_lost() is called.
        """
        self._transport = transport
        self._set_transport_info()
        _LOGGER.info(
            "%s: Smart meter connected to %s", self._instance_id(), self._transport_info
        )

    def connection_lost(self, exc: Optional[Exception]) -> None:
        """Called when the connection is lost or closed.

        The argument is an exception object or None (the latter
        meaning a regular EOF is received or the connection was
        aborted or closed).
        """
        if exc:
            _LOGGER.warning(
                "%s: Connection to %s lost: %s",
                self._instance_id(),
                self._transport_info,
                exc,
            )
        else:
            _LOGGER.debug(
                "%s: Connection to %s closed",
                self._instance_id(),
                self._transport_info,
            )

        if self._transport:
            try:
                self._transport.close()
                self._transport = None
            except Exception as ex:
                _LOGGER.warning(
                    "%s: Error when closing transport %s for %s connection: %s",
                    self._instance_id(),
                    self._transport_info,
                    "lost" if exc else "closed",
                    ex,
                )

        self.done.set_result(True)

    def data_received(self, data: bytes) -> None:
        """Called when some data is received.

        The argument is a bytes object.
        """
        frames = self._frame_reader.read(data)
        for frame in frames:
            self.queue.put_nowait(frame)

    def eof_received(self) -> bool:
        """Called when the other end signals it won’t send any more data"""
        _LOGGER.debug(
            "%s: eof_received - the other end (%s) signaled it won’t send any more data. Close transport",
            self._instance_id(),
            self._transport_info,
        )

        # return false to close transport.
        return False

    def _instance_id(self) -> str:
        return f"{self.__class__.__name__}[{self.id}]"


class ConnectionManager:
    """
    Maintain connection and reconnect if connection is lost.
    Reconnecting uses a back-off retry strategy, and has a simple circuit breaker for connection lost.
    """

    DEFAULT_CONNECTION_LOST_BACK_OFF_THRESHOLD: int = 5
    DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC: int = 5

    def __init__(
        self, connection_factory: Callable[[], Tuple[BaseTransport, BaseProtocol]],
    ) -> None:
        """
        Initialize class.
        :param connection_factory: A factory function that returns a Transport and SmartMeterProtocol tuple.
        """

        self._connection_factory: Callable[
            [], Tuple[BaseTransport, BaseProtocol]
        ] = connection_factory
        self._connection: Optional[Tuple[BaseTransport, BaseProtocol]] = None
        self._is_closing: Event = Event()

        self.back_off_connect_error: BackOffStrategy = ExponentialBackOff()

        self.connection_lost_back_off_threshold = (
            ConnectionManager.DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC
        )
        self.connection_lost_back_off_sleep_sec = (
            ConnectionManager.DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC
        )
        self._connection_lost_last_time: Optional[datetime.datetime] = None
        self._connection_lost_sleep_before_reconnect = False

    def close(self):
        """Close current connection, if any, and stop reconnecting."""
        self._is_closing.set()
        if self._connection:
            _LOGGER.info("Close connection and abort connect loop")
            transport, _ = self._connection
            transport.close()
            self._connection = None

    async def connect_loop(self):
        """
        Connect to meter using connection factory, and keep reconnecting if connection is lost.
        The connection is not reconnected on connection loss if close() was called on this instance.
        """
        while not self._is_closing.is_set():
            await asyncio.wait(
                [self._try_connect(), self._is_closing.wait()],
                return_when=FIRST_COMPLETED,
            )

            if self._connection:
                _, protocol = self._connection
                await asyncio.wait(
                    [protocol.done, self._is_closing.wait()],
                    return_when=FIRST_COMPLETED,
                )

                if not self._is_closing.is_set():
                    _LOGGER.warning("Connection lost")
                    self._update_connection_lost_circuit_breaker()

                self._connection = None

        # done closing if that was the case of connection loss
        self._is_closing.clear()

        _LOGGER.info("Connect loop done")

    def _update_connection_lost_circuit_breaker(self):
        now = datetime.datetime.utcnow()
        if self._connection_lost_last_time:
            delta = now - self._connection_lost_last_time
            self._connection_lost_sleep_before_reconnect = (
                delta.total_seconds() < self.connection_lost_back_off_threshold
            )
        self._connection_lost_last_time = now

    async def _try_connect(self):
        current_connect_error_delay = self.back_off_connect_error.current_delay_sec
        if (
            current_connect_error_delay > 0
            or self._connection_lost_sleep_before_reconnect
        ):
            reconnect_sleep = (
                self.connection_lost_back_off_sleep_sec
                if self._connection_lost_sleep_before_reconnect
                else 0
            )
            sleep_time = max(current_connect_error_delay, reconnect_sleep)

            _LOGGER.info(
                "Back-off for %d sec before reconnecting", sleep_time,
            )
            await asyncio.sleep(sleep_time)

        if not self._is_closing.is_set():
            try:
                _LOGGER.debug("Try to connect")
                self._connection = await self._connection_factory()
                self.back_off_connect_error.reset()
            except CancelledError:
                raise
            except Exception as ex:
                self._connection = None
                self.back_off_connect_error.failure()
                _LOGGER.warning("Error connecting: %s", ex)


class BackOffStrategy(metaclass=ABCMeta):
    """
    Back-off strategy base class.
    Create sub-classes to implement different strategies.
    """

    DEFAULT_MAX_DELAY_SEC = 60

    @abstractmethod
    def failure(self) -> None:
        """Call this after a failure."""
        pass

    @abstractmethod
    def reset(self) -> None:
        """Call this after success to reset."""
        pass

    @property
    @abstractmethod
    def current_delay_sec(self) -> int:
        """Current back-off delay in seconds."""
        pass


class ExponentialBackOff(BackOffStrategy):
    """Exponential back-off strategy."""

    def __init__(self) -> None:
        self._delay = 0
        self.max_delay = super().DEFAULT_MAX_DELAY_SEC

    def failure(self) -> None:
        self._delay = self._delay * 2
        if self._delay == 0:
            self._delay = 1

    def reset(self) -> None:
        self._delay = 0

    @property
    def current_delay_sec(self) -> int:
        return self._delay if self._delay < self.max_delay else self.max_delay


async def create_meter_tcp_connection(
    queue: Queue, loop: Optional[AbstractEventLoop] = None, *args, **kwargs
) -> Tuple[BaseTransport, BaseProtocol]:
    """
    Create TCP connection using :class:`SmartMeterProtocol`

    :param queue: Queue for received frames
    :param loop: The event handler
    :param args: Passed to the :class:`loop.create_connection`
    :param kwargs: Passed to the :class:`loop.create_connection`
    :return: Tuple of transport and protocol
    """
    loop = loop if loop else asyncio.get_event_loop()
    return await loop.create_connection(
        lambda: typing.cast(BaseProtocol, SmartMeterProtocol(queue)), *args, **kwargs
    )