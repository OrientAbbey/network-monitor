"""
Réduction du trafic HTTP du dashboard : ETag (réponse 304 sans corps quand rien n'a changé) et
`Cache-Control: no-cache` (le navigateur revalide à chaque fois, donc ne sert jamais de donnée
périmée). Middleware ASGI pur : il ne touche ni aux flux ni aux requêtes autres que GET.
La compression (gzip) est ajoutée par `GZipMiddleware`, **autour** de celui-ci : l'ETag se
calcule sur le corps non compressé.
"""

from __future__ import annotations

import hashlib

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_MAX_BUFFERED = 4 * 1024 * 1024  # au-delà, on ne calcule pas d'ETag (réponses volumineuses : exports)


class ETagMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return

        candidates = {c.strip() for c in Headers(scope=scope).get("if-none-match", "").split(",") if c.strip()}
        start: Message | None = None
        chunks: list[bytes] = []
        size = 0
        passthrough = False

        async def wrapper(message: Message) -> None:
            nonlocal start, size, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = Headers(raw=message["headers"])
                cacheable = message["status"] == 200 and "etag" not in headers
                if not cacheable:
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            # http.response.body
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if size > _MAX_BUFFERED:  # trop gros : on vide ce qu'on a retenu et on laisse passer la suite
                passthrough = True
                assert start is not None
                await send(start)
                await send({"type": "http.response.body", "body": b"".join(chunks), "more_body": message.get("more_body", False)})
                return
            if message.get("more_body", False):
                return
            assert start is not None
            body = b"".join(chunks)
            etag = f'W/"{hashlib.sha1(body, usedforsecurity=False).hexdigest()[:20]}"'
            headers = MutableHeaders(raw=start["headers"])
            headers["etag"] = etag
            headers["cache-control"] = "no-cache"
            if etag in candidates or "*" in candidates:
                for header in ("content-length", "content-type", "content-encoding"):
                    if header in headers:
                        del headers[header]
                await send({"type": "http.response.start", "status": 304, "headers": start["headers"]})
                await send({"type": "http.response.body", "body": b""})
                return
            headers["content-length"] = str(len(body))
            await send(start)
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapper)
