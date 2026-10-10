# ADR 0001 — Python і офіційний MCP SDK 2.x

Статус: прийнято (2026-10-10)

**Контекст.** Власник обрав Python. Потрібні числові бібліотеки (numpy), асинхронні біржові клієнти (ccxt), у перспективі `ib_async`.

**Рішення.** Python ≥3.11, `mcp>=2.2` з `mcp.server.mcpserver.MCPServer`, httpx (async), pydantic, ccxt, numpy. Менеджер залежностей — uv, збірка — hatchling. Два транспорти: stdio (локально, Claude Desktop/Code) і streamable-http (віддалений режим).

**Наслідки.** Lifespan викликається на кожне з'єднання → провайдери переживають `aclose()` (інваріант №7). Тести — in-process `mcp.Client(server)`. Описи інструментів англійською.

**Альтернативи.** TypeScript (як у DOUBLETOP) — відхилено: гірше для квант-обчислень. FastMCP 2 (сторонній) — відхилено на користь офіційного SDK.
