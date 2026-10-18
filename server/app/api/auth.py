from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import AppError
from ..models import User
from ..security import bearer_token, current_user, issue_token, revoke_token, verify_password
from .schemas import LoginIn, LoginOut, UserOut
from ..services import preferences

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)) -> LoginOut:
    user = db.scalar(select(User).where(func.lower(User.email) == body.email))
    if user is None or not verify_password(user.password_hash, body.password):
        raise AppError(401, "invalid_credentials", "email or password is incorrect")
    token = issue_token(db, user)
    db.commit()
    return LoginOut(token=token, user=UserOut.model_validate(user))


@router.post("/logout", status_code=204)
def logout(token: str = Depends(bearer_token), db: Session = Depends(get_db)) -> None:
    revoke_token(db, token)
    db.commit()


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(current_user)) -> User:
    return user


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ai_autonomy: Literal["auto", "step"]
    expected_version: int = Field(ge=1)


@router.get("/preferences")
def get_preferences(user: User = Depends(current_user)) -> dict:
    return preferences.out(user)


@router.patch("/preferences")
def patch_preferences(body: PreferencesIn, user: User = Depends(current_user), db: Session = Depends(get_db)) -> dict:
    result = preferences.update(db, user, **body.model_dump())
    db.commit()
    return result
