"""An in-memory Slack conversation, served through :class:`httpx.MockTransport`."""

import httpx
from botwatch import slack
from botwatch.slack import _api
from dataclasses import dataclass
from dataclasses import field

CONVERSATION = 'C0123456789'


@dataclass
class FakeSlack:
    """One conversation's history and threads, answering
    ``conversations.history`` and ``conversations.replies`` the way Slack does:
    history newest first, replies oldest first with the parent atop every page,
    and both in pages of ``page_size``."""

    page_size: int = 2

    history: list[_api.Message] = field(default_factory=list)
    """Top-level messages, oldest first."""

    replies: dict[str, list[_api.Message]] = field(default_factory=dict)
    """Each thread's replies, oldest first, by the parent's ``ts``."""

    requests: list[httpx.Request] = field(default_factory=list)

    def post(
        self,
        ts: str,
        *,
        thread_ts: str | None = None,
        user: str = 'U0456ABC',
        text: str = 'hello',
    ) -> _api.Message:
        message = _api.Message(
            ts=ts,
            thread_ts=thread_ts,
            user=user,
            text=text,
        )
        if thread_ts is None:
            self.history.append(message)
        else:
            self.replies.setdefault(thread_ts, []).append(message)
        return message

    def _list_matching(
        self,
        method: str,
        params: httpx.QueryParams,
    ) -> list[_api.Message]:
        oldest = params.get('oldest', '0')
        if method == 'conversations.history':
            matching = [
                message for message in reversed(self.history) if message.ts > oldest
            ]
        else:
            thread = self.replies.get(params['ts'], [])
            matching = [message for message in thread if message.ts > oldest]
        return matching

    def _get_parent(self, thread_ts: str) -> _api.Message:
        """A thread's parent as replies return it, with its newest reply."""
        parent = next(message for message in self.history if message.ts == thread_ts)
        replies = self.replies.get(thread_ts, [])
        return parent.model_copy(
            update={'latest_reply': replies[-1].ts if replies else None},
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit('/', 1)[-1]
        matching = self._list_matching(method, request.url.params)

        offset = int(request.url.params.get('cursor') or 0)
        end = offset + min(self.page_size, int(request.url.params['limit']))
        page = matching[offset:end]
        if method == 'conversations.replies':
            page = [self._get_parent(request.url.params['ts']), *page]

        return httpx.Response(
            200,
            json={
                'ok': True,
                'messages': [message.model_dump(exclude_none=True) for message in page],
                'response_metadata': {
                    'next_cursor': str(end) if end < len(matching) else '',
                },
            },
        )

    def create_client(self) -> slack.SlackClient:
        return slack.SlackClient(
            'xoxb-test',
            transport=httpx.MockTransport(self._handle),
        )
