"""Standalone server: the voice-agent API under /voice-agent and the web app at /."""

import logging
import os
import pathlib

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from routes import public_router, router  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
app = FastAPI(title="Ava · dental voice agent")
app.include_router(public_router)
app.include_router(router)
FRONTEND = pathlib.Path(__file__).resolve().parent.parent / "frontend"
app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")
