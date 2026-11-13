"""Ponto de entrada do uvicorn: `uvicorn fraud.api.main:app`."""

from fraud.api.app import create_app

app = create_app()
