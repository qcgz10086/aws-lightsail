from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.deps import COOKIE_NAME, get_current_user
from app.models import User
from app.password_strength import validate_strong_password
from app.rate_limit import login_limiter, rate_limit_key
from app.schemas import ChangePasswordRequest, LoginRequest, TokenResponse, UserOut
from app.security import check_dummy_password, create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        is_admin=user.is_admin,
        monthly_limit_gb=user.monthly_limit_gb,
        auto_stop_on_limit_default=bool(user.auto_stop_on_limit_default),
        created_at=user.created_at,
        has_credentials=bool(user.credentials),
        credential_count=len(user.credentials or []),
    )


def _set_auth_cookie(response: Response, token: str) -> None:
    """R-5：登录成功后用 HttpOnly Cookie 下发 token。"""
    settings = get_settings()
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _clear_auth_cookie(response: Response) -> None:
    """R-5：登出时清除登录 Cookie。"""
    settings = get_settings()
    response.delete_cookie(
        key=COOKIE_NAME,
        path="/",
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
    )


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_not_locked(request: Request, username: str) -> str:
    """R-2：同一 IP+用户名失败过多时返回 429；返回本次请求的限流 key。"""
    key = rate_limit_key(_client_ip(request), username)
    retry_after = login_limiter.check_locked(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="失败次数过多，请稍后再试",
            headers={"Retry-After": str(retry_after)},
        )
    return key


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)
) -> TokenResponse:
    # R-2：同一 IP+用户名 15 分钟内累计失败 5 次后锁定 15 分钟，锁定期间返回 429。
    key = _check_not_locked(request, body.username)
    user = db.query(User).filter(User.username == body.username).first()
    if not user:
        # R-10：用户不存在也做一次 bcrypt 校验，避免时序侧信道泄露用户是否存在。
        check_dummy_password(body.password)
        login_limiter.record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    if not verify_password(body.password, user.password_hash):
        login_limiter.record_failure(key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误")
    login_limiter.record_success(key)
    token = create_access_token(
        user.username,
        extra={"uid": user.id, "adm": user.is_admin},
        token_version=user.token_version,
    )
    # R-5：token 同时经 HttpOnly Cookie 下发；响应体里仍返回 access_token 供 API 客户端使用。
    _set_auth_cookie(response, token)
    return TokenResponse(access_token=token, user=_user_out(user))


@router.post("/logout")
def logout(
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # R-3/R-5：登出即递增 token_version（此前签发的全部 token 失效），并清除 Cookie。
    user.token_version += 1
    db.add(user)
    db.commit()
    _clear_auth_cookie(response)
    return {"message": "已登出"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    # R-2：改密同样按 IP+用户名限流；R-4：新密码须满足与实例密码同一强度标准。
    key = _check_not_locked(request, user.username)
    if not verify_password(body.old_password, user.password_hash):
        login_limiter.record_failure(key)
        raise HTTPException(status_code=400, detail="原密码不正确")
    try:
        validate_strong_password(body.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    login_limiter.record_success(key)
    user.password_hash = hash_password(body.new_password)
    # R-3：改密后此前签发的全部 token 失效，须重新登录。
    user.token_version += 1
    db.add(user)
    db.commit()
    return {"message": "密码已修改"}
