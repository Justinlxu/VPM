"""Dashboard FastAPI backend."""

import sys
import os
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Ensure project root on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from Dashboard.backend.routers import dashboard, history, leaderboard, predict
from Dashboard.backend.ws.price_feed import price_feed


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start WebSocket price polling
    task = asyncio.create_task(price_feed.poll_loop())
    yield
    price_feed.stop()
    task.cancel()


app = FastAPI(title="VPM Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# REST routers
app.include_router(dashboard.router)
app.include_router(history.router)
app.include_router(leaderboard.router)
app.include_router(predict.router)


# WebSocket endpoint
@app.websocket("/ws/prices")
async def websocket_prices(ws: WebSocket):
    await price_feed.connect(ws)
    try:
        while True:
            # Keep connection alive; client can send pings
            await ws.receive_text()
    except WebSocketDisconnect:
        price_feed.disconnect(ws)


@app.get("/api/health")
def health():
    return {"status": "ok"}
