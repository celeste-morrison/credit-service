"""BodySizeLimit at the ASGI level, with crafted headers and bodies."""

from starlette.types import Message, Receive, Scope, Send

from app.api.middleware import BodySizeLimit

LIMIT = 100


async def read_body_then_ok(scope: Scope, receive: Receive, send: Send) -> None:
    """A minimal app that reads the whole body, then responds 200."""
    more = True
    while more:
        message = await receive()
        more = message.get("more_body", False)
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def call(headers: list[tuple[bytes, bytes]], chunks: list[bytes]) -> tuple[int, int]:
    """Run the middleware and return (status sent, body bytes the app read)."""
    pending: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    read = 0
    sent: list[Message] = []

    async def receive() -> Message:
        nonlocal read
        message = pending.pop(0) if pending else {"type": "http.disconnect"}
        read += len(message.get("body", b""))
        return message

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {"type": "http", "method": "POST", "path": "/credits", "headers": headers}
    await BodySizeLimit(read_body_then_ok, LIMIT)(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    return status, read


async def test_body_within_limit_passes() -> None:
    assert await call([(b"content-length", b"10")], [b"x" * 10]) == (200, 10)


async def test_declared_length_over_limit_is_413_without_reading() -> None:
    assert await call([(b"content-length", b"101")], [b"x" * 101]) == (413, 0)


async def test_any_transfer_encoding_without_length_is_411() -> None:
    for value in (b"chunked", b"Chunked", b"gzip, CHUNKED"):
        status, read = await call([(b"transfer-encoding", value)], [b"x" * 10])
        assert (status, read) == (411, 0)


async def test_undeclared_body_is_cut_off_at_limit() -> None:
    status, read = await call([], [b"x" * 60] * 100)

    assert status == 413
    assert read <= LIMIT + 60


async def test_body_longer_than_declared_is_cut_off() -> None:
    status, read = await call([(b"content-length", b"10")], [b"x" * 60] * 100)

    assert status == 413
    assert read <= LIMIT + 60
