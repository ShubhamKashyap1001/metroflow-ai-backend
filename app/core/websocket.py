"""Thin re-export so routes/services can `from app.core.websocket import
manager` without depending on the app/websocket package path directly."""
from app.websocket.manager import manager              
from app.websocket import events              
