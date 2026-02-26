import os

from dotenv import load_dotenv


load_dotenv()


def _env_bool(name: str, default: str = "false") -> bool:
    return str(os.getenv(name, default)).strip().lower() in {"1", "true", "yes", "on"}


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/")
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "600"))

MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_DB = os.getenv("MYSQL_DB", "fortune_telling")
MYSQL_USER = os.getenv("MYSQL_USER", "fortune_app")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "fortune_app_dev")

VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", "./local_qdrand")
VECTOR_COLLECTION_NAME = os.getenv("VECTOR_COLLECTION_NAME", "local_documents")

SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY")
YUANFENJU_API_KEY = os.getenv("YUANFENJU_API_KEY")
SMS_DEBUG_CODE_ENABLED = _env_bool("SMS_DEBUG_CODE_ENABLED", "true")
