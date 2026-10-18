"""Deterministic DXF writes with ezdxf.

``ezdxf.options.write_fixed_meta_data_for_testing`` (fixed ``$TDCREATE`` /
``$TDUPDATE``, GUIDs and the WRITTEN_BY marker) is a process-global switch.
Every writer that needs it (the drawing sheets, the DWG adapter's R2000
normalisation) sets it inside :func:`fixed_metadata`, which holds one lock for
the whole set / write / restore: without it a concurrent writer could restore
the old value in the middle of another write (non-deterministic bytes) or
leave the switch on for the rest of the process.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

import ezdxf

_FIXED_METADATA_LOCK = threading.RLock()


@contextmanager
def fixed_metadata() -> Iterator[None]:
    """Write with ezdxf's fixed metadata; the switch is restored on exit (lock held throughout)."""
    with _FIXED_METADATA_LOCK:
        previous = ezdxf.options.write_fixed_meta_data_for_testing
        ezdxf.options.write_fixed_meta_data_for_testing = True
        try:
            yield
        finally:
            ezdxf.options.write_fixed_meta_data_for_testing = previous
