"""Внутренние компоненты Alice CLI/server.

Тяжёлые модули (manager, transport, gateway) не импортируются здесь, чтобы
не создавать цикл с alice_server.py. Импортируйте напрямую:
    from tools.effective import normalize_effective_mode
    from tools.parser    import parse_nsb_card, compute_delta
    from tools.manager   import AliceAgentManager, AliceMcpServer, SessionRegistry
    from tools.transport import AliceWsEngine, AliceRpcClient
    from tools.gateway   import ModelCatalog, attach_openai_routes
"""
__version__ = "0.1.0"
