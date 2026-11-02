"""One heavy model at a time (resource rule 10).

The coding worker process runs both the coding-agent loop and the self-repair loop; each
holds HEAVY_MODEL_LOCK for a whole task (model call and tests), so a code task and a repair
never run a model, or a test suite, concurrently on the 4-core box.
"""

from __future__ import annotations

import asyncio

HEAVY_MODEL_LOCK = asyncio.Lock()
