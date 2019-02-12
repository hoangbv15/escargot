class ClientError(Exception):
	pass

class ServerError(Exception):
	pass

class GroupNameTooLong(ClientError):
	pass

class GroupDoesNotExist(ClientError):
	pass

class CannotRemoveSpecialGroup(ClientError):
	pass

class ContactDoesNotExist(ClientError):
	pass

class ContactAlreadyOnList(ClientError):
	pass

class NicknameExceedsLengthLimit(ClientError):
	pass

class EmptyDomainInXXL(ClientError):
	pass

class InvalidXXLPayload(ClientError):
	pass

class ContactNotOnList(ClientError):
	pass

class UserDoesNotExist(ClientError):
	pass

class ContactNotOnline(ClientError):
	pass

class AuthFail(ClientError):
	pass

class NotAllowedWhileHDN(ClientError):
	pass
