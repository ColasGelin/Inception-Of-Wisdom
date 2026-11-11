"""Runtime settings, read once from the environment at import time."""

import os

APP_NAME = os.environ.get("APP_NAME", "hello-iow")
GREETING = os.environ.get("APP_GREETING", "Hello")
MAX_NAME_LENGTH = 40
