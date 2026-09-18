from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer


async def require_server_token(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> None:
    token = request.app.state.config.server.token
    if token is None:
        return
    if credentials is None or not secrets.compare_digest(
        credentials.credentials.encode(), token.get_secret_value().encode()
    ):
        raise HTTPException(401, "Invalid or missing bearer token", headers={"WWW-Authenticate": "Bearer"})
