from typing import TYPE_CHECKING, Callable, Optional, Dict, Any, List
from abc import ABCMeta, abstractmethod
from .models import User, Contact, Lst, MessageData, TextWithData, Substatus, LoginOption

if TYPE_CHECKING:
	from .backend import BackendSession, Chat, ChatSession

class BackendEventHandler(metaclass = ABCMeta):
	__slots__ = ('bs',)
	
	bs: 'BackendSession'
	
	# Note to subclassers, regarding `__init__`:
	# `bs` is assigned in `Backend.login`, before `BackendEventHandler.on_open` is called,
	# because of circular references.
	# Therefore, your `__init__` should be conspicuously missing an assignment to `bs`.
	
	def on_open(self) -> None:
		pass
	
	def on_close(self) -> None:
		pass
	
	def on_system_message(self, *args: Any, **kwargs: Any) -> None:
		pass
	
	def on_maintenance_boot(self) -> None:
		pass
	
	@abstractmethod
	def on_presence_notification(self, ctc_head: User, old_substatus: Substatus, on_contact_add: bool, *, trid: Optional[str] = None, update_status: bool = True, send_status_on_bl: bool = False, visible_notif: bool = True, updated_phone_info: Optional[Dict[str, Any]] = None, circle_user_bs: Optional['BackendSession'] = None, circle_id: Optional[str] = None) -> None: pass
	
	@abstractmethod
	def on_chat_invite(self, chat: 'Chat', inviter: User, *, inviter_id: Optional[str] = None, invite_msg: str = '') -> None: pass
	
	# `user` added me to their FL, and they're now on my RL.
	@abstractmethod
	def on_added_me(self, user: User, *, adder_id: Optional[str] = None, message: Optional[TextWithData] = None) -> None: pass
	
	# `user` didn't accept contact request
	@abstractmethod
	def on_contact_request_denied(self, user_added: User, message: str, *, contact_id: Optional[str] = None) -> None: pass
	
	@abstractmethod
	def on_login_elsewhere(self, option: LoginOption) -> None: pass
	
	# TODO: Make these non-frontend-specific to allow interop
	
	def msn_on_oim_sent(self, oim_uuid: str) -> None:
		pass
	
	def msn_on_oim_deletion(self) -> None:
		pass
	
	def msn_on_uun_sent(self, sender: User, snm: bytes) -> None:
		pass
	
	def msn_on_put_sent(self, message: 'Message', sender: User, *, pop_id_sender: Optional[str] = None, pop_id: Optional[str] = None) -> None:
		pass
	
	def msn_on_user_circle_presence(self, bs_other: 'BackendSession') -> None:
		pass
	
	def ymsg_on_xfer_init(self, yahoo_data: Dict[str, Any]) -> None:
		pass
	
	def ymsg_on_upload_file_ft(self, recipient: str, message: str) -> None:
		pass
		
	def ymsg_on_sent_ft_http(self, yahoo_id_sender: str, url_path: str, upload_time: int, message: str) -> None:
		pass

class ChatEventHandler(metaclass = ABCMeta):
	__slots__ = ('cs',)
	
	cs: 'ChatSession'
	
	# Note to subclassers, regarding `__init__`:
	# `cs` is assigned in `Chat.join`, before `ChatEventHandler.on_open` is called,
	# because of circular references.
	# Therefore, your `__init__` should be conspicuously missing an assignment to `cs`.
	
	def on_open(self) -> None:
		pass
	
	def on_close(self, keep_future: bool, idle: bool) -> None:
		pass
	
	@abstractmethod
	def on_participant_joined(self, cs_other: 'ChatSession') -> None: pass
	
	@abstractmethod
	def on_participant_left(self, cs_other: 'ChatSession', idle: bool, last_pop: bool) -> None: pass
	
	@abstractmethod
	def on_invite_declined(self, invited_user: User, *, invited_id: Optional[str] = None, message: str = '') -> None: pass
	
	@abstractmethod
	def on_message(self, data: MessageData) -> None: pass
