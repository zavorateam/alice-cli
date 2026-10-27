"""WS + RPC транспорт.

Пока реэкспорт из alice_server. Полная деинтеграция — отдельным патчем,
когда alice_server.py станет тонким lifecycle-модулем.
"""
from alice_server import AliceRpcClient, AliceWsEngine

__all__ = ["AliceRpcClient", "AliceWsEngine"]
