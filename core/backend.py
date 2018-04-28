from typing import Dict, List, Set, Any, Tuple, Optional, Sequence, FrozenSet, Iterable
from abc import ABCMeta, abstractmethod
import asyncio, time, traceback
from collections import defaultdict
from enum import IntFlag

from util.misc import gen_uuid, EMPTY_SET, run_loop, Runner

from .user import UserService
from .auth import AuthService
from .stats import Stats
from .client import Client
from .models import User, UserDetail, Group, Lst, Contact, UserStatus, TextWithData, MessageData, Substatus, LoginOption
from . import error, event

class Ack(IntFlag):
	Zero = 0
	NAK = 1
	ACK = 2
	Full = 3

class Backend:
	__slots__ = (
		'user_service', 'auth_service', 'loop', 'notify_maintenance', 'maintenance_mode', 'maintenance_mins',  '_stats', '_sc',
		'_chats_by_id', '_user_by_uuid', '_worklist_sync_db', '_worklist_notify', '_runners', '_dev',
	)
	
	user_service: UserService
	auth_service: AuthService
	loop: asyncio.AbstractEventLoop
	notify_maintenance: bool
	maintenance_mode: bool
	maintenance_mins: int
	_stats: Stats
	_sc: '_SessionCollection'
	_chats_by_id: Dict[Tuple[str, str], 'Chat']
	_user_by_uuid: Dict[str, User]
	_worklist_sync_db: Dict[User, UserDetail]
	_worklist_notify: Dict[str, Tuple['BackendSession', Substatus]]
	_runners: List[Runner]
	_dev: Optional[Any]
	
	def __init__(self, loop: asyncio.AbstractEventLoop, *, user_service: Optional[UserService] = None, auth_service: Optional[AuthService] = None) -> None:
		self.user_service = user_service or UserService()
		self.auth_service = auth_service or AuthService()
		self.loop = loop
		self.notify_maintenance = False
		self.maintenance_mode = False
		self.maintenance_mins = 0
		self._stats = Stats()
		self._sc = _SessionCollection()
		self._chats_by_id = {}
		self._user_by_uuid = {}
		self._worklist_sync_db = {}
		self._worklist_notify = {}
		self._runners = []
		self._dev = None
		
		loop.create_task(self._worker_sync_db())
		loop.create_task(self._worker_clean_sessions())
		loop.create_task(self._worker_sync_stats())
		loop.create_task(self._worker_notify())
	
	def push_system_message(self, *args: Any, message: str = '', **kwargs: Any) -> None:
		for bs in self._sc.iter_sessions():
			bs.evt.on_system_message(*args, message = message, **kwargs)
		
		if isinstance(args[1], int) and args[1] > 0:
			self.notify_maintenance = True
			self.maintenance_mins = args[1]
			self.loop.create_task(self._worker_set_server_maintenance())
	
	async def _worker_set_server_maintenance(self):
		while self.maintenance_mins > 0:
			await asyncio.sleep(60)
			self.maintenance_mins -= 1
		
		if self.maintenance_mins <= 0:
			self.notify_maintenance = False
			self.maintenance_mode = True
			for bs in self._sc._sessions.copy():
				bs.evt.on_maintenance_boot()
	
	def add_runner(self, runner: Runner) -> None:
		self._runners.append(runner)
	
	def run_forever(self) -> None:
		run_loop(self.loop, self._runners)
	
	def on_leave(self, sess: 'BackendSession') -> None:
		user = sess.user
		old_substatus = user.status.substatus
		self._stats.on_logout()
		self._sc.remove_session(sess)
		if self._sc.get_sessions_by_user(user):
			# There are still other people logged in as this user,
			# so don't send offline notifications.
			return
		# User is offline, send notifications
		user.status.substatus = Substatus.Offline
		self._sync_contact_statuses(user)
		user.detail = None
		self._notify_contacts(sess, old_substatus = old_substatus)
	
	def login(self, uuid: str, client: Client, evt: event.BackendEventHandler, option: LoginOption) -> Optional['BackendSession']:
		user = self._load_user_record(uuid)
		if user is None: return None
		self.user_service.update_date_login(uuid)
		
		bs_others = list(self._sc.get_sessions_by_user(user))
		for bs_other in bs_others:
			try:
				bs_other.evt.on_login_elsewhere(option)
			except:
				traceback.print_exc()
		
		bs = BackendSession(self, user, client, evt)
		bs.evt.bs = bs
		self._stats.on_login()
		self._stats.on_user_active(user, client)
		self._sc.add_session(bs)
		user.detail = self._load_detail(user)
		bs.evt.on_open()
		return bs
	
	def _load_user_record(self, uuid: str) -> Optional[User]:
		if uuid not in self._user_by_uuid:
			user = self.user_service.get(uuid)
			if user is None: return None
			self._user_by_uuid[uuid] = user
		return self._user_by_uuid[uuid]
	
	def _load_detail(self, user: User) -> UserDetail:
		if user.detail: return user.detail
		detail = self.user_service.get_detail(user.uuid)
		assert detail is not None
		return detail
	
	def chat_create(self) -> 'Chat':
		return Chat(self, self._stats)
	
	def chat_get(self, scope: str, id: str) -> Optional['Chat']:
		return self._chats_by_id.get((scope, id))
	
	def _sync_contact_statuses(self, user: User) -> None:
		detail = user.detail
		if detail is None: return
		for ctc in detail.contacts.values():
			if ctc.lists & Lst.FL:
				ctc.compute_visible_status(user)
			
			# If the contact lists ever become inconsistent (FL without matching RL),
			# the contact that's missing the RL will always see the other user as offline.
			# Because of this, and the fact that most contacts *are* two-way, and it
			# not being that much extra work, I'm leaving this line commented out.
			#if not ctc.lists & Lst.RL: continue
			
			if ctc.head.detail is None: continue
			ctc_rev = ctc.head.detail.contacts.get(user.uuid)
			if ctc_rev is None: continue
			ctc_rev.compute_visible_status(ctc.head)
	
	def _notify_contacts(self, bs: 'BackendSession', *, old_substatus: Substatus) -> None:
		uuid = bs.user.uuid
		if uuid in self._worklist_notify:
			return
		self._worklist_notify[uuid] = (bs, old_substatus)
	
	def _mark_modified(self, user: User, *, detail: Optional[UserDetail] = None) -> None:
		ud = user.detail or detail
		if detail: assert ud is detail
		assert ud is not None
		self._worklist_sync_db[user] = ud
	
	def util_get_uuid_from_email(self, email: str) -> Optional[str]:
		return self.user_service.get_uuid(email)
	
	def util_set_sess_token(self, sess: 'BackendSession', token: str) -> None:
		self._sc.set_nc_by_token(sess, token)
	
	def util_get_sess_by_token(self, token: str) -> Optional['BackendSession']:
		return self._sc.get_nc_by_token(token)
	
	def util_get_sessions_by_user(self, user: User) -> Iterable['BackendSession']:
		return self._sc.get_sessions_by_user(user)
	
	def dev_connect(self, obj: object) -> None:
		if self._dev is None: return
		self._dev.connect(obj)
	
	def dev_disconnect(self, obj: object) -> None:
		if self._dev is None: return
		self._dev.disconnect(obj)
	
	async def _worker_sync_db(self) -> None:
		while True:
			await asyncio.sleep(1)
			self._sync_db_impl()
	
	def _sync_db_impl(self) -> None:
		if not self._worklist_sync_db: return
		try:
			users = list(self._worklist_sync_db.keys())[:100]
			batch = []
			for user in users:
				detail = self._worklist_sync_db.pop(user, None)
				if not detail: continue
				batch.append((user, detail))
			self.user_service.save_batch(batch)
		except:
			traceback.print_exc()
	
	async def _worker_clean_sessions(self) -> None:
		while True:
			await asyncio.sleep(10)
			now = time.time()
			closed = []
			
			try:
				for sess in self._sc.iter_sessions():
					if sess.closed:
						closed.append(sess)
			except:
				traceback.print_exc()
			
			for sess in closed:
				self._sc.remove_session(sess)
	
	async def _worker_sync_stats(self) -> None:
		while True:
			await asyncio.sleep(60)
			try:
				self._stats.flush()
			except:
				traceback.print_exc()
	
	async def _worker_notify(self) -> None:
		# Notify relevant `BackendSession`s of status, name, message, media, etc. changes
		worklist = self._worklist_notify
		while True:
			await asyncio.sleep(0.2)
			try:
				for bs, old_substatus in worklist.values():
					user = bs.user
					detail = self._load_detail(user)
					for ctc in detail.contacts.values():
						for bs_other in self._sc.get_sessions_by_user(ctc.head):
							detail_other = bs_other.user.detail
							if detail_other is None: continue
							ctc_me = detail_other.contacts.get(user.uuid)
							# This shouldn't be `None`, since every contact should have
							# an `RL` contact on the other users' list (at the very least).
							if ctc_me is None: continue
							if not ctc_me.lists & Lst.FL: continue
							bs_other.evt.on_presence_notification(ctc_me, old_substatus)
			except:
				traceback.print_exc()
			worklist.clear()

class Session(metaclass = ABCMeta):
	__slots__ = ('closed',)
	
	closed: bool
	
	def __init__(self) -> None:
		self.closed = False
	
	def close(self) -> None:
		if self.closed:
			return
		self.closed = True
		self._on_close()
	
	@abstractmethod
	def _on_close(self) -> None: pass

class BackendSession(Session):
	__slots__ = ('backend', 'user', 'client', 'evt', 'front_data')
	
	backend: Backend
	user: User
	client: Client
	evt: event.BackendEventHandler
	front_data: Dict[str, Any]
	
	def __init__(self, backend: Backend, user: User, client: Client, evt: event.BackendEventHandler) -> None:
		super().__init__()
		self.backend = backend
		self.user = user
		self.client = client
		self.evt = evt
		self.front_data = {}
	
	def _on_close(self) -> None:
		self.evt.on_close()
		self.backend.on_leave(self)
	
	def me_update(self, fields: Dict[str, Any]) -> None:
		user = self.user
		detail = user.detail
		assert detail is not None
		
		needs_notify = False
		
		old_substatus = user.status.substatus
		
		if 'message' in fields:
			user.status.message = fields['message']
			needs_notify = True
		if 'media' in fields:
			user.status.media = fields['media']
			needs_notify = True
		if 'name' in fields:
			user.status.name = fields['name']
			needs_notify = True
		if 'blp' in fields:
			detail.settings['blp'] = fields['blp']
			needs_notify = True
		if 'substatus' in fields:
			user.status.substatus = fields['substatus']
			if old_substatus != user.status.substatus:
				needs_notify = True
		if 'gtc' in fields:
			detail.settings['gtc'] = fields['gtc']
		
		self.backend._mark_modified(user)
		if needs_notify:
			self.backend._sync_contact_statuses(user)
			self.backend._notify_contacts(self, old_substatus = old_substatus)
	
	def me_group_add(self, name: str, *, is_favorite: Optional[bool] = None) -> Group:
		if len(name) > MAX_GROUP_NAME_LENGTH:
			raise error.GroupNameTooLong()
		user = self.user
		detail = user.detail
		assert detail is not None
		group = Group(_gen_group_id(detail), name, is_favorite = is_favorite)
		detail.groups[group.id] = group
		self.backend._mark_modified(user)
		return group
	
	def me_group_remove(self, group_id: str) -> None:
		if group_id == '0':
			raise error.CannotRemoveSpecialGroup()
		user = self.user
		detail = user.detail
		assert detail is not None
		try:
			del detail.groups[group_id]
		except KeyError:
			raise error.GroupDoesNotExist()
		for ctc in detail.contacts.values():
			ctc.groups.discard(group_id)
		self.backend._mark_modified(user)
	
	def me_group_edit(self, group_id: str, new_name: str, *, is_favorite: Optional[bool] = None) -> None:
		user = self.user
		detail = user.detail
		assert detail is not None
		g = detail.groups.get(group_id)
		if g is None:
			raise error.GroupDoesNotExist()
		if new_name is not None:
			if len(new_name) > MAX_GROUP_NAME_LENGTH:
				raise error.GroupNameTooLong()
			g.name = new_name
		if is_favorite is not None:
			g.is_favorite = is_favorite
		self.backend._mark_modified(user)
	
	def me_group_contact_add(self, group_id: str, contact_uuid: str) -> None:
		if group_id == '0': return
		user = self.user
		detail = user.detail
		assert detail is not None
		if group_id not in detail.groups:
			raise error.GroupDoesNotExist()
		ctc = detail.contacts.get(contact_uuid)
		if ctc is None:
			raise error.ContactDoesNotExist()
		if group_id in ctc.groups:
			raise error.ContactAlreadyOnList()
		ctc.groups.add(group_id)
		self.backend._mark_modified(user)
	
	def me_group_contact_remove(self, group_id: str, contact_uuid: str) -> None:
		user = self.user
		detail = user.detail
		assert detail is not None
		ctc = detail.contacts.get(contact_uuid)
		if ctc is None:
			raise error.ContactDoesNotExist()
		if group_id not in detail.groups and group_id != '0':
			raise error.GroupDoesNotExist()
		try:
			ctc.groups.remove(group_id)
		except KeyError:
			if group_id == '0':
				raise error.ContactNotOnList()
		self.backend._mark_modified(user)
	
	def me_contact_add(self, contact_uuid: str, lst: Lst, *, name: Optional[str] = None, message: Optional[TextWithData] = None) -> Tuple[Contact, User]:
		backend = self.backend
		ctc_head = backend._load_user_record(contact_uuid)
		if ctc_head is None:
			raise error.UserDoesNotExist()
		user = self.user
		ctc = self._add_to_list(user, ctc_head, lst, name)
		if lst & Lst.FL:
			# FL needs a matching RL on the contact
			ctc_me = self._add_to_list(ctc_head, user, Lst.RL, user.status.name)
			# If other user hasn't already allowed/blocked me, notify them that I added them to my list.
			if not ctc_me.lists & (Lst.AL | Lst.BL):
				# `ctc_head` was added to `user`'s RL
				for sess_added in backend._sc.get_sessions_by_user(ctc_head):
					if sess_added is self: continue
					sess_added.evt.on_added_me(user, message = message)
		self.evt.on_presence_notification(ctc, old_substatus = Substatus.Offline)
		backend._notify_contacts(self, old_substatus = Substatus.Offline)
		return ctc, ctc_head
	
	def me_contact_edit(self, contact_uuid: str, *, is_messenger_user: Optional[bool] = None) -> None:
		user = self.user
		detail = user.detail
		assert detail is not None
		ctc = detail.contacts.get(contact_uuid)
		if ctc is None:
			raise error.ContactDoesNotExist()
		
		updated = False
		
		orig_is_messenger_user = ctc.is_messenger_user
		if is_messenger_user is not None:
			ctc.is_messenger_user = is_messenger_user
		
		if is_messenger_user != orig_is_messenger_user:
			updated = True
		
		if updated:
			self.backend._mark_modified(user)
	
	def me_contact_remove(self, contact_uuid: str, lst: Lst) -> None:
		user = self.user
		detail = user.detail
		assert detail is not None
		ctc = detail.contacts.get(contact_uuid)
		if ctc is None:
			raise error.ContactDoesNotExist()
		assert not lst & Lst.RL
		self._remove_from_list(user, ctc.head, lst)
		if lst & Lst.FL:
			ctc.groups = set()
			# Remove matching RL
			self._remove_from_list(ctc.head, user, Lst.RL)
	
	def me_contact_deny(self, adder_uuid: str, deny_message: Optional[str]):
		user_adder = self.backend._load_user_record(adder_uuid)
		if user_adder is None:
			raise error.UserDoesNotExist()
		user = self.user
		for sess_adder in self.backend._sc.get_sessions_by_user(user_adder):
			if sess_adder is self: continue
			sess_adder.evt.on_contact_request_denied(user, deny_message or '')
	
	def _add_to_list(self, user: User, ctc_head: User, lst: Lst, name: Optional[str]) -> Contact:
		# Add `ctc_head` to `user`'s `lst`
		detail = self.backend._load_detail(user)
		contacts = detail.contacts
		
		updated = False
		
		if ctc_head.uuid not in contacts:
			contacts[ctc_head.uuid] = Contact(ctc_head, set(), Lst.Empty, UserStatus(name))
			updated = True
		ctc = contacts[ctc_head.uuid]
		
		orig_name = ctc.status.name
		if ctc.status.name is None:
			ctc.status.name = name
		
		if orig_name != name:
			updated = True
		
		# If I add someone to FL, and they're not already blocked,
		# they should also be added to AL.
		if lst == Lst.FL and not ctc.lists & Lst.BL:
			lst = lst | Lst.AL
		
		if (ctc.lists & lst) != lst:
			ctc.lists |= lst
			updated = True
		
		if updated:
			self.backend._mark_modified(user, detail = detail)
			self.backend._sync_contact_statuses(user)
		
		return ctc
	
	def _remove_from_list(self, user: User, ctc_head: User, lst: Lst) -> None:
		# Remove `ctc_head` from `user`'s `lst`
		detail = self.backend._load_detail(user)
		contacts = detail.contacts
		ctc = contacts.get(ctc_head.uuid)
		if ctc is None: return
		
		updated = False
		if ctc.lists & lst:
			ctc.lists &= ~lst
			updated = True
		
		if not ctc.lists:
			del contacts[ctc_head.uuid]
			updated = True
		
		if updated:
			self.backend._mark_modified(user, detail = detail)
			self.backend._sync_contact_statuses(user)
	
	def me_contact_notify_oim(self, uuid: str, oim_uuid: str) -> None:
		ctc_head = self.backend._load_user_record(uuid)
		if ctc_head is None:
			raise error.UserDoesNotExist()
		
		for sess_notify in self.backend._sc.get_sessions_by_user(ctc_head):
			if sess_notify is self: continue
			sess_notify.evt.msn_on_oim_sent(uuid)
	
	def me_send_uun_invitation(self, uuid: str, snm: bytes):
		ctc_head = self.backend._load_user_record(uuid)
		if ctc_head is None:
			raise error.UserDoesNotExist()
		
		for sess_notify in self.backend._sc.get_sessions_by_user(ctc_head):
			if sess_notify is self: continue
			sess_notify.evt.msn_on_uun_sent(self.user, snm)

class _SessionCollection:
	__slots__ = ('_sessions', '_sessions_by_user', '_sess_by_token', '_tokens_by_sess')
	
	_sessions: Set[BackendSession]
	_sessions_by_user: Dict[User, Set[BackendSession]]
	_sess_by_token: Dict[str, BackendSession]
	_tokens_by_sess: Dict[BackendSession, Set[str]]
	
	def __init__(self) -> None:
		self._sessions = set()
		self._sessions_by_user = defaultdict(set)
		self._sess_by_token = {}
		self._tokens_by_sess = defaultdict(set)
	
	def get_sessions_by_user(self, user: User) -> Iterable[BackendSession]:
		if user not in self._sessions_by_user:
			return EMPTY_SET
		return self._sessions_by_user[user]
	
	def iter_sessions(self) -> Iterable[BackendSession]:
		yield from self._sessions
	
	def set_nc_by_token(self, sess: BackendSession, token: str) -> None:
		self._sess_by_token[token] = sess
		self._tokens_by_sess[sess].add(token)
		self._sessions.add(sess)
	
	def get_nc_by_token(self, token: str) -> Optional[BackendSession]:
		return self._sess_by_token.get(token)
	
	def add_session(self, sess: BackendSession) -> None:
		if sess.user:
			self._sessions_by_user[sess.user].add(sess)
		self._sessions.add(sess)
	
	def remove_session(self, sess: BackendSession) -> None:
		if sess in self._tokens_by_sess:
			tokens = self._tokens_by_sess.pop(sess)
			for token in tokens:
				self._sess_by_token.pop(token, None)
		self._sessions.discard(sess)
		if sess.user in self._sessions_by_user:
			self._sessions_by_user[sess.user].discard(sess)

class Chat:
	__slots__ = ('ids', 'backend', 'front_data', '_users_by_sess', '_stats')
	
	ids: Dict[str, str]
	backend: Backend
	front_data: Dict[str, Any]
	_users_by_sess: Dict['ChatSession', User]
	_stats: Any
	
	def __init__(self, backend: Backend, stats: Any) -> None:
		super().__init__()
		self.ids = {}
		self.backend = backend
		self.front_data = {}
		self._users_by_sess = {}
		self._stats = stats
		
		self.add_id('main', gen_uuid())
	
	def add_id(self, scope: str, id: str):
		assert id not in self.backend._chats_by_id
		self.ids[scope] = id
		self.backend._chats_by_id[(scope, id)] = self
	
	def join(self, origin: str, bs: BackendSession, evt: event.ChatEventHandler) -> 'ChatSession':
		cs = ChatSession(origin, bs, self, evt)
		cs.evt.cs = cs
		self._users_by_sess[cs] = cs.user
		cs.evt.on_open()
		return cs
	
	def add_session(self, sess: 'ChatSession') -> None:
		self._users_by_sess[sess] = sess.user
	
	def get_roster(self) -> Iterable['ChatSession']:
		return self._users_by_sess.keys()
	
	def send_participant_joined(self, cs: 'ChatSession') -> None:
		for cs_other in self.get_roster():
			if cs_other is cs and cs.origin is 'yahoo': continue
			cs_other.evt.on_participant_joined(cs)
	
	def on_leave(self, sess: 'ChatSession') -> None:
		su = self._users_by_sess.pop(sess, None)
		if su is None: return
		# TODO: If it goes down to only 1 connected user,
		# the chat and remaining session(s) should be automatically closed.
		if not self._users_by_sess:
			for scope_id in self.ids.items():
				del self.backend._chats_by_id[scope_id]
			return
		# Notify others that `sess` has left
		for sess1, _ in self._users_by_sess.items():
			if sess1 is sess: continue
			sess1.evt.on_participant_left(sess)

class ChatSession(Session):
	__slots__ = ('origin', 'user', 'chat', 'bs', 'evt')
	
	origin: Optional[str]
	user: User
	chat: Chat
	bs: BackendSession
	evt: event.ChatEventHandler
	
	def __init__(self, origin: str, bs: BackendSession, chat: Chat, evt: event.ChatEventHandler) -> None:
		super().__init__()
		self.origin = origin
		self.user = bs.user
		self.chat = chat
		self.bs = bs
		self.evt = evt
	
	def _on_close(self) -> None:
		self.evt.on_close()
		self.chat.on_leave(self)
	
	def invite(self, invitee_uuid: str, *, invite_msg: Optional[str] = None) -> None:
		detail = self.user.detail
		assert detail is not None
		ctc = detail.contacts.get(invitee_uuid)
		if ctc is None:
			if self.user.uuid != invitee_uuid: raise error.ContactDoesNotExist()
			invitee = self.user
		else:
			if ctc.status.is_offlineish(): raise error.ContactNotOnline()
			invitee = ctc.head
		ctc_sessions = self.bs.backend.util_get_sessions_by_user(invitee)
		for ctc_sess in ctc_sessions:
			ctc_sess.evt.on_chat_invite(self.chat, self.user, invite_msg = invite_msg or '')
	
	def send_message_to_everyone(self, data: MessageData) -> None:
		stats = self.chat._stats
		client = self.bs.client
		
		stats.on_message_sent(self.user, client)
		stats.on_user_active(self.user, client)
		
		for cs_other in self.chat._users_by_sess.keys():
			if cs_other is self: continue
			cs_other.evt.on_message(data)
			stats.on_message_received(cs_other.user, client)
	
	def send_message_to_user(self, user_uuid: str, data: MessageData) -> None:
		stats = self.chat._stats
		client = self.bs.client
		
		stats.on_message_sent(self.user, client)
		stats.on_user_active(self.user, client)
		
		for cs_other in self.chat._users_by_sess.keys():
			if cs_other is self: continue
			if cs_other.user.uuid != user_uuid: continue
			cs_other.evt.on_message(data)
			stats.on_message_received(cs_other.user, client)

def _gen_group_id(detail: UserDetail) -> str:
	id = 1
	s = str(id)
	while s in detail.groups:
		id += 1
		s = str(id)
	return s
	
MAX_GROUP_NAME_LENGTH = 61
