"""
GenCall configuration manager.
Reads gencall.cfg and provides typed access to all settings.
"""

import configparser
import os
import logging

logger = logging.getLogger("gencall.config")

DEFAULT_CONFIG_PATH = "/opt/gencall/etc/gencall.cfg"
LOCAL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "etc", "gencall.cfg")


class Config:
    _instance = None

    def __new__(cls, path=None):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self, path=None):
        if self._initialized:
            return
        self._parser = configparser.ConfigParser()
        self._path = path or self._find_config()
        if self._path:
            self._parser.read(self._path)
            logger.info("Config loaded from %s", self._path)
        else:
            logger.warning("No config file found, using defaults")
        self._initialized = True

    @staticmethod
    def _find_config():
        for p in [os.environ.get("GENCALL_CONFIG", ""), LOCAL_CONFIG_PATH, DEFAULT_CONFIG_PATH]:
            if p and os.path.isfile(p):
                return p
        return None

    def get(self, section, key, fallback=None):
        return self._parser.get(section, key, fallback=fallback)

    def getint(self, section, key, fallback=0):
        return self._parser.getint(section, key, fallback=fallback)

    def getfloat(self, section, key, fallback=0.0):
        return self._parser.getfloat(section, key, fallback=fallback)

    def getbool(self, section, key, fallback=False):
        return self._parser.getboolean(section, key, fallback=fallback)

    # --- Web ---
    @property
    def web_host(self):
        return self.get("web", "host", "0.0.0.0")

    @property
    def web_port(self):
        return self.getint("web", "port", 8080)

    @property
    def web_ssl(self):
        return self.getbool("web", "ssl", False)

    @property
    def ssl_cert(self):
        return self.get("web", "ssl_cert", "")

    @property
    def ssl_key(self):
        return self.get("web", "ssl_key", "")

    # --- SIP ---
    @property
    def sip_t1(self):
        return self.getint("sip", "T1", 60)

    @property
    def sip_t2(self):
        return self.getint("sip", "T2", 120)

    @property
    def min_rtp_port(self):
        return self.getint("sip", "min_rtp_port", 10000)

    @property
    def max_rtp_port(self):
        return self.getint("sip", "max_rtp_port", 20000)

    # --- SIPp ---
    @property
    def sipp_command(self):
        return self.get("sipp", "command", "/usr/local/bin/sipp")

    @property
    def sipp_file_limit(self):
        return self.getint("sipp", "open_file_limit", 5000)

    @property
    def sipp_transport(self):
        return self.get("sipp", "default_transport", "udp")

    # --- Database ---
    # Secrets (DB credentials) should come from the environment, never the
    # config file. Env vars override the corresponding [database] settings.
    @property
    def db_engine(self):
        return os.environ.get("GENCALL_DB_ENGINE") or self.get("database", "engine", "sqlite")

    @property
    def db_url(self):
        # Full URL override wins (e.g. GENCALL_DATABASE_URL=postgresql://...).
        env_url = os.environ.get("GENCALL_DATABASE_URL")
        if env_url:
            return env_url

        if self.db_engine == "postgresql":
            user = os.environ.get("GENCALL_PG_USER") or self.get("database", "pg_user", "gencall")
            pw = os.environ.get("GENCALL_PG_PASSWORD") or self.get("database", "pg_password", "")
            host = os.environ.get("GENCALL_PG_HOST") or self.get("database", "pg_host", "127.0.0.1")
            port = os.environ.get("GENCALL_PG_PORT") or self.getint("database", "pg_port", 5432)
            db = os.environ.get("GENCALL_PG_DATABASE") or self.get("database", "pg_database", "gencall")
            return f"postgresql://{user}:{pw}@{host}:{port}/{db}"
        else:
            path = self.get("database", "sqlite_path", "/opt/gencall/etc/gencall.db")
            return f"sqlite:///{path}"

    # --- Logging ---
    @property
    def log_level(self):
        return self.getint("logging", "level", 20)

    @property
    def log_file(self):
        return self.get("logging", "file", "/opt/gencall/logs/gencall.log")

    # --- Media ---
    @property
    def media_path(self):
        return self.get("media", "path", "/opt/gencall/media")

    # --- Stats ---
    @property
    def stats_interval(self):
        return self.getint("stats", "interval", 5)

    @property
    def stats_history_size(self):
        return self.getint("stats", "history_size", 1000)

    @classmethod
    def reset(cls):
        cls._instance = None
