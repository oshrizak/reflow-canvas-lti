"""Canvas REST API client.

Thin async wrapper around the subset of the Canvas API the integration
needs: list course PDFs, download a file, create/publish/delete a Page,
send a Conversation message.

Keep this client narrow. Every method that takes a ``course_id`` or
``file_id`` is a thin shim over the Canvas REST endpoint of the same
name — that one-to-one mapping makes failures easy to debug against the
Canvas API docs.
"""

from .client import CanvasClient
from .errors import CanvasApiError

__all__ = ["CanvasClient", "CanvasApiError"]
