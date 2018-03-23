from typing import Optional, Callable
import asyncio
import struct

from aiohttp import web
from core.backend import Backend
from util.misc import Logger

from .ymsg_ctrl import YMSGCtrlBase

def register(loop: asyncio.AbstractEventLoop, backend: Backend, http_app: web.Application) -> None:
	from util.misc import ProtocolRunner
	from . import pager, http
	
	backend.add_runner(ProtocolRunner('0.0.0.0', 5050, ListenerYMSG, args = ['YH', backend, pager.YMSGCtrlPager]))
	http.register(http_app)

class ListenerYMSG(asyncio.Protocol):
	logger: Logger
	backend: Backend
	controller: YMSGCtrlBase
	transport: Optional[asyncio.WriteTransport]
	
	def __init__(self, logger_prefix: str, backend: Backend, controller_factory: Callable[[Logger, str, Backend], YMSGCtrlBase]) -> None:
		super().__init__()
		self.logger = Logger(logger_prefix, self)
		self.backend = backend
		self.controller = controller_factory(self.logger, 'direct', backend)
		self.controller.close_callback = self._on_close
		self.transport = None
	
	def connection_made(self, transport: asyncio.BaseTransport) -> None:
		assert isinstance(transport, asyncio.WriteTransport)
		self.transport = transport
		self.logger.log_connect()
	
	def connection_lost(self, exc: Exception) -> None:
		self.controller.close()
		self.logger.log_disconnect()
		self.transport = None
	
	def data_received(self, data: bytes) -> None:
		transport = self.transport
		assert transport is not None
		self.controller.transport = None
		self.controller.data_received(transport, data)
		transport.write(self.controller.flush())
		self.controller.transport = transport
	
	def _on_close(self) -> None:
		if self.transport is None: return
		self.transport.close()
