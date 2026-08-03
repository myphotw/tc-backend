from pydantic import BaseModel


class ApiKeyCreate(BaseModel):
    service_name: str
    api_key: str
    description: str | None = None
