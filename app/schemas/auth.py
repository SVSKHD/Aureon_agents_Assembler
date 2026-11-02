from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# Whitespace is stripped from the email only; passwords are taken verbatim.
Email = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$"
    ),
]


class LoginRequest(BaseModel):
    email: Email
    password: str = Field(min_length=1, max_length=1024)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    last_login_at: datetime | None


class LoginResponse(BaseModel):
    user: UserOut
    # Same value as the CSRF cookie; send it back in the X-CSRF-Token header.
    csrf_token: str
    expires_at: datetime


class RevokeAllResponse(BaseModel):
    revoked: int
