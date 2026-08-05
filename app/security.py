from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings, get_settings

security = HTTPBasic(auto_error=False)


def require_admin(
    credentials: HTTPBasicCredentials | None = Depends(security),
    settings: Settings = Depends(get_settings),
) -> str:
    if not settings.auth_enabled:
        return settings.admin_username
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Autenticazione richiesta",
            headers={"WWW-Authenticate": "Basic"},
        )
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_password = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Credenziali non valide",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
