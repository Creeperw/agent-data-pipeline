"""Local web console for the agent distillation data pipeline.

The package is a thin orchestration + inspection layer on top of the existing
``agent/*`` scripts. It never imports torch/transformers/openai and never
modifies an existing pipeline script: every action is executed as a separate
``python -m agent.<module>`` subprocess, exactly like ``pipeline_launcher.py``.

Start it with:

    python -m agent.ui --port 8770

See ``agent/ui/README.md`` for the full guide.
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.0"
