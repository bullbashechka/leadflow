"""Local development settings. Do not use for public deployment."""

from .base import *  # noqa: F403
from .base import env

DEBUG = True
CRM_AUTH_MODE = env("CRM_AUTH_MODE", default="demo")
