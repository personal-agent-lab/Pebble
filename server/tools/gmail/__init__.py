"""Gmail 工具包：认证、协议客户端、查询、校验与草稿准备。"""

from server.tools.gmail.client import (
    BaseGmailClient,
    GmailMessage,
    GoogleApiGmailClient,
    MockGmailClient,
    SendReplyResult,
    get_gmail_client,
)
from server.tools.gmail.service import ReplyDraftStore
from server.tools.gmail.tools import (
    format_thread_transcript,
    get_email_detail,
    get_email_thread,
    prepare_reply,
    query_emails,
    read_reply_draft,
    update_reply_draft,
)
from server.tools.gmail.validator import (
    ValidationError,
    ValidationResult,
    validate_reply_draft,
)

__all__ = [
    "BaseGmailClient",
    "GmailMessage",
    "GoogleApiGmailClient",
    "MockGmailClient",
    "ReplyDraftStore",
    "SendReplyResult",
    "ValidationError",
    "ValidationResult",
    "format_thread_transcript",
    "get_email_detail",
    "get_email_thread",
    "get_gmail_client",
    "prepare_reply",
    "query_emails",
    "read_reply_draft",
    "update_reply_draft",
    "validate_reply_draft",
]
