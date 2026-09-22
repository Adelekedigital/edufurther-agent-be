"""Root entrypoint.

`uv run fastapi run main.py` from the repository root, matching the AI
Router's Railway start command. Importing `src` onto the path here keeps the
application package itself free of path manipulation.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from app.core.eventloop import install_selector_event_loop_policy  # noqa: E402

# Before the app is imported and before any loop exists: the Postgres
# checkpointer runs on psycopg, which cannot use Windows' default loop.
install_selector_event_loop_policy()

from app.main import app  # noqa: E402

__all__ = ["app"]
