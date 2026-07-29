from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str

    class Config:
        env_file = ".env"


settings = Settings()

print(settings.POSTGRES_HOST)
print(settings.POSTGRES_PORT)
print(settings.POSTGRES_DB)
print(settings.POSTGRES_USER)