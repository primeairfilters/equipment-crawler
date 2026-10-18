"""The runtime connection: SELECT/INSERT/UPDATE only. Callers commit."""
import os

import pymysql


def connect():
    return pymysql.connect(
        host=os.environ.get("EQ_DB_HOST", "db"),
        port=int(os.environ.get("EQ_DB_PORT", "3306")),
        user=os.environ["EQ_RUNTIME_USER"],
        password=os.environ["EQ_RUNTIME_PASSWORD"],
        database=os.environ.get("EQ_DB_NAME", "tracforce_equipment"),
        autocommit=False,
        connect_timeout=10,
    )
