"""Gmail 工具包：认证、协议客户端、查询、校验与草稿准备。"""

from server.tools.gmail.client import (
    BaseGmailClient,
    GmailMessage,
    GoogleApiGmailClient,
    MockGmailClient,
    SendReplyResult,
    effective_reply_recipients,
    get_gmail_client,
    recipients_match_reply_target,
)
from server.tools.gmail.protocol import (
    DraftSaveResult,
    DraftStorageProtocol,
    InMemoryDraftStorage,
    get_draft_storage,
    set_draft_storage,
)
from server.tools.gmail.tools import (
    format_thread_transcript,
    get_email_detail,
    get_email_thread,
    prepare_reply,
    query_emails,
)
from server.tools.gmail.validator import (
    ValidationError,
    ValidationResult,
    validate_reply_draft,
)

__all__ = [
    "BaseGmailClient",
    "DraftSaveResult",
    "DraftStorageProtocol",
    "GmailMessage",
    "GoogleApiGmailClient",
    "InMemoryDraftStorage",
    "MockGmailClient",
    "SendReplyResult",
    "ValidationError",
    "ValidationResult",
    "effective_reply_recipients",
    "format_thread_transcript",
    "get_draft_storage",
    "get_email_detail",
    "get_email_thread",
    "get_gmail_client",
    "prepare_reply",
    "query_emails",
    "recipients_match_reply_target",
    "set_draft_storage",
    "validate_reply_draft",
]
