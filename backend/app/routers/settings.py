from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_current_admin, get_current_user
from app.models import User
from app.services.query_settings import get_query_settings, save_query_settings
from app.services.read_throttle import read_throttle
from app.settings_models import (MAX_QUERY_INTERVAL_MS, MAX_TRAFFIC_INTERVAL_MINUTES,
                                 MIN_QUERY_INTERVAL_MS, MIN_TRAFFIC_INTERVAL_MINUTES)

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_interval_ms: StrictInt = Field(ge=MIN_QUERY_INTERVAL_MS, le=MAX_QUERY_INTERVAL_MS)
    traffic_interval_minutes: StrictInt = Field(ge=MIN_TRAFFIC_INTERVAL_MINUTES, le=MAX_TRAFFIC_INTERVAL_MINUTES)


class SettingsView(SettingsUpdate):
    next_sync_at: datetime
    updated_at: datetime


@router.get("", response_model=SettingsView)
def read_settings(db: Session = Depends(get_db), _user: User = Depends(get_current_user)):
    return get_query_settings(db)


@router.put("", response_model=SettingsView)
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db),
                    user: User = Depends(get_current_admin)):
    try:
        result = save_query_settings(db, user.id, user.token_version,
                                     payload.query_interval_ms, payload.traffic_interval_minutes)
    except PermissionError:
        raise HTTPException(status_code=403, detail="管理员权限已变化，请重新登录") from None
    read_throttle.settings_changed()
    return result
