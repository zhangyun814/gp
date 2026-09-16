from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://planet:planet@localhost:5432/planet_stock"
    knowledge_planet_token: str = ""
    market_data_provider: str = "csv"
    market_data_csv: str = "data/quotes.csv"
    rise_threshold: float = 0.10
    rise_window_days: int = 5
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
