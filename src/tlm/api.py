from __future__ import annotations

from pathlib import Path

from .config import load_symbols


def create_app():
    try:
        from fastapi import FastAPI
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "FastAPI is not installed. Install the API extras before running the server."
        ) from exc

    app = FastAPI(title="Trading LLM Agent", version="0.1.0")

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/data/symbols")
    def data_symbols(config_dir: str = "configs") -> dict:
        symbols = load_symbols(Path(config_dir))
        return {
            "symbols": {
                alias: {
                    "provider": symbol.provider,
                    "instrument": symbol.instrument,
                    "description": symbol.description,
                    "price_scale": symbol.price_scale,
                }
                for alias, symbol in symbols.items()
            }
        }

    return app


app = create_app()
