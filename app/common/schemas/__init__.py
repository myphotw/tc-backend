from datetime import datetime

from pydantic import BaseModel, ConfigDict, model_validator

from app.common.services.key_resolver import ExternalServiceName


class ApiKeyCreate(BaseModel):
    service_name: ExternalServiceName
    api_key: str
    description: str | None = None
    enabled: bool = True


class ApiKeyUpdate(BaseModel):
    api_key: str | None = None
    description: str | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_update(self) -> "ApiKeyUpdate":
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class ApiKeyStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_name: str
    configured: bool
    enabled: bool
    description: str | None = None
    masked: str = "****"
    created_at: datetime | None = None
    updated_at: datetime | None = None
