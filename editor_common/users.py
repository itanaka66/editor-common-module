"""Multi-user account storage shared by both editors: a `UserMixin` each
app's own declarative model can inherit from, plus DB-agnostic
create/authenticate/change-password helpers.

Each app still defines its own concrete model (own table name, own
relationships to its Project rows if any) — this module only owns the
columns and the logic that touches them, so it has no dependency on either
app's `Base`::

    # app/models.py
    from editor_common.users import UserMixin
    from .db import Base

    class User(Base, UserMixin):
        __tablename__ = "users"
"""
import datetime
from typing import Optional

from sqlalchemy import DateTime, String, select
from sqlalchemy.orm import Mapped, mapped_column

from .passwords import hash_password, needs_rehash, verify_password


class UserMixin:
    """Columns for a multi-user account usable with both HTTP Basic Auth
    and OAuth2 (Google/GitHub) login.

    `password_hash` is nullable: an account created via `create_user`
    (Basic Auth) always has one, but one created via `get_or_create_oauth_user`
    (first Google/GitHub login) has none — there's no password to check, so
    `authenticate_user` always fails closed for it, which is correct: that
    account can only sign in through OAuth2.

    `email` is nullable+unique and only ever set by the OAuth2 path (see
    `get_or_create_oauth_user`) — it's how a second login from the same
    provider (or a different one, e.g. Google after GitHub) is recognized
    as the same account rather than creating a duplicate.

    `is_admin` is provided for apps that want to gate a subset of endpoints
    (e.g. user management itself) to a subset of accounts; apps that don't
    need that distinction can just ignore the column.
    """

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(150), unique=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True, nullable=True)
    is_admin: Mapped[bool] = mapped_column(default=False)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.datetime.now(datetime.timezone.utc)
    )


class UsernameTakenError(ValueError):
    pass


class UserNotFoundError(ValueError):
    pass


def create_user(db, user_model: type, username: str, password: str, *, is_admin: bool = False):
    username = username.strip()
    if not username or not password:
        raise ValueError("username and password are required")
    existing = db.scalar(select(user_model).where(user_model.username == username))
    if existing is not None:
        raise UsernameTakenError(f"username {username!r} is already taken")
    user = user_model(username=username, password_hash=hash_password(password), is_admin=is_admin)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db, user_model: type, username: str, password: str):
    """Returns the matching, active user row, or None. On success with a
    hash made under an older (weaker) iteration count, transparently
    re-hashes and commits the stronger one.

    Always returns None for an OAuth2-only account (`password_hash` is
    None) — there is no password to check it against, so Basic Auth can
    never authenticate it; that account signs in through OAuth2 only.
    """
    user = db.scalar(select(user_model).where(user_model.username == username.strip()))
    if user is None or not user.is_active or not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        db.commit()
    return user


def change_password(db, user_model: type, username: str, new_password: str) -> None:
    user = db.scalar(select(user_model).where(user_model.username == username.strip()))
    if user is None:
        raise UserNotFoundError(f"no such user {username!r}")
    if not new_password:
        raise ValueError("new_password is required")
    user.password_hash = hash_password(new_password)
    db.commit()


def set_active(db, user_model: type, username: str, is_active: bool) -> None:
    user = db.scalar(select(user_model).where(user_model.username == username.strip()))
    if user is None:
        raise UserNotFoundError(f"no such user {username!r}")
    user.is_active = is_active
    db.commit()


def delete_user(db, user_model: type, username: str) -> None:
    user = db.scalar(select(user_model).where(user_model.username == username.strip()))
    if user is None:
        raise UserNotFoundError(f"no such user {username!r}")
    db.delete(user)
    db.commit()


def list_users(db, user_model: type) -> list:
    return list(db.scalars(select(user_model).order_by(user_model.username)).all())


def ensure_bootstrap_user(db, user_model: type, username: str, password: str, *, is_admin: bool = True) -> Optional[object]:
    """Creates `username` as an admin if the user table is empty — so a
    fresh deployment isn't locked out before anyone has run a user-creation
    command. No-op (returns None) once at least one user row exists."""
    if db.scalar(select(user_model.id).limit(1)) is not None:
        return None
    return create_user(db, user_model, username, password, is_admin=is_admin)


def _unique_username_from_email(db, user_model: type, email: str) -> str:
    base = (email.split("@", 1)[0] or "user").strip() or "user"
    candidate = base
    suffix = 1
    while db.scalar(select(user_model.id).where(user_model.username == candidate)) is not None:
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def get_or_create_oauth_user(db, user_model: type, email: str, *, display_name: str | None = None, is_admin: bool = False):
    """First-login auto-registration for OAuth2 (Google/GitHub — see
    `editor_common.oauth`): looks up an existing account by `email` and
    returns it, or creates a new one with no password (see `UserMixin`'s
    docstring for why that's correct) and a username derived from the email
    local-part, de-duplicated against existing usernames.

    `display_name` is accepted but not stored — `UserMixin` has no separate
    display-name column, so apps that want to show one (e.g. "田中さん" next
    to "tanaka42") should add their own column and set it here via a
    subclass override, or in the `get_or_create_user` callback passed to
    `editor_common.oauth.register_oauth_routes`.
    """
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    user = db.scalar(select(user_model).where(user_model.email == email))
    if user is not None:
        return user
    user = user_model(
        username=_unique_username_from_email(db, user_model, email),
        password_hash=None,
        email=email,
        is_admin=is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
