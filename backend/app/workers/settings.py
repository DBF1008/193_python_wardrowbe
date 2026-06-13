from arq.connections import RedisSettings

from app.config import get_settings

settings = get_settings()


def get_redis_settings() -> RedisSettings:
    redis_url = str(settings.redis_url)
    parts = redis_url.replace("redis://", "").split("/")
    host_port = parts[0]
    database = int(parts[1]) if len(parts) > 1 else 0

    if ":" in host_port:
        host, port = host_port.split(":")
        port = int(port)
    else:
        host = host_port
        port = 6379

    return RedisSettings(host=host, port=port, database=database)
