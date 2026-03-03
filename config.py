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
DIFY_BASE_URL = os.getenv("DIFY_BASE_URL", "http://localhost/v1").rstrip("/")
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_WORKFLOW_APP_ID = os.getenv("DIFY_WORKFLOW_APP_ID")
MEDIA_GEN_ENABLED = _env_bool("MEDIA_GEN_ENABLED", "false")
MEDIA_INTENT_ROUTER_V2 = _env_bool("MEDIA_INTENT_ROUTER_V2", "true")
MEDIA_INTENT_ROUTER_V3 = _env_bool("MEDIA_INTENT_ROUTER_V3", "true")
MEDIA_INTENT_LLM_FALLBACK = _env_bool("MEDIA_INTENT_LLM_FALLBACK", "true")
MEDIA_INTENT_NEGATION_GUARD = _env_bool("MEDIA_INTENT_NEGATION_GUARD", "true")
MEDIA_TIMEOUT_SECONDS = int(os.getenv("MEDIA_TIMEOUT_SECONDS", "180"))
MEDIA_POLL_INTERVAL_SECONDS = int(os.getenv("MEDIA_POLL_INTERVAL_SECONDS", "4"))
SMS_DEBUG_CODE_ENABLED = _env_bool("SMS_DEBUG_CODE_ENABLED", "true")
SMS_PROVIDER = str(os.getenv("SMS_PROVIDER", "mock")).strip().lower()
SMS_HTTP_TIMEOUT_SECONDS = int(os.getenv("SMS_HTTP_TIMEOUT_SECONDS", "8"))
SMS_ALIYUN_ACCESS_KEY_ID = os.getenv("SMS_ALIYUN_ACCESS_KEY_ID", "")
SMS_ALIYUN_ACCESS_KEY_SECRET = os.getenv("SMS_ALIYUN_ACCESS_KEY_SECRET", "")
SMS_ALIYUN_SIGN_NAME = os.getenv("SMS_ALIYUN_SIGN_NAME", "")
SMS_ALIYUN_TEMPLATE_CODE = os.getenv("SMS_ALIYUN_TEMPLATE_CODE", "")
SMS_ALIYUN_REGION_ID = os.getenv("SMS_ALIYUN_REGION_ID", "cn-hangzhou")
SMS_ALIYUN_ENDPOINT = os.getenv("SMS_ALIYUN_ENDPOINT", "dysmsapi.aliyuncs.com")
SMS_TEMPLATE_PARAM_CODE_KEY = os.getenv("SMS_TEMPLATE_PARAM_CODE_KEY", "code")
