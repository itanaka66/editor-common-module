"""Shared server-side building blocks for the Integrated Writers Editor and
Integrated Novel Editor FastAPI backends.

Everything here is factored out of the parts of both apps that were
byte-for-byte identical or differed only in a handful of app-specific
constants (app name, storage dir, Qdrant collection name, ...). Each helper
takes those small differences as explicit parameters/callables instead of
importing an app's own `config`/`models` modules, so this package has no
dependency on either consuming app.
"""

__version__ = "0.1.0"
