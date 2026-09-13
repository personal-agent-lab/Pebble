"""Gmail 工具包：认证、协议客户端、查询与发送。"""

from server.tools.gmail.client import (
    BaseGmailClient,
    GmailMessage,
    GoogleApiGmailClient,
    MockGmailClient,
    SendReplyResult,
    get_gmail_client,
)
from server.tools.gmail.tools import (
    format_thread_transcript,
    get_email_detail,
    get_email_thread,
    query_emails,
)

__all__ = [
    "BaseGmailClient",
    "GmailMessage",
    "GoogleApiGmailClient",
    "MockGmailClient",
    "SendReplyResult",
    "format_thread_transcript",
    "get_email_detail",
    "get_email_thread",
    "get_gmail_client",
    "query_emails",
]
