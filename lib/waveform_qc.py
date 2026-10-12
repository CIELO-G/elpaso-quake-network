"""Quality guards applied to raw counts before any consumer touches them.

Raw storage keeps what the server sent (see ingest A11/A13). Consumers must
not trust it blindly: a Raspberry Shake record that fails its Steim2
integrity check decodes to a handful of absurd samples (2026-10-06, AM.R0F2D,
three samples near 5e8 counts against a normal 1.7e4). A bandpass filter
smears such a sample into a ringing wavelet that PhaseNet happily picks.

The guard is deliberately dumb: every digitiser in the network is 24-bit, so
``|counts| > 2**23`` cannot be a real sample. Those samples become gaps, and
the consumer's existing ``merge(fill_value="interpolate")`` bridges them.
"""

from __future__ import annotations

import numpy as np
from obspy import Stream

# 24-bit ADC full scale. Raspberry Shake (RS1D/RS3D/RS4D) and the KIDD
# digitiser are all 24-bit; a sample outside this range is corruption.
MAX_ABS_COUNTS = 2**23


def mask_impossible_samples(st: Stream, *, max_abs: int = MAX_ABS_COUNTS, logger=None) -> int:
    """Turn physically impossible integer samples into gaps, in place.

    Only integer-typed traces are inspected (processed/float data has
    already been through this guard). Returns the number of samples masked.
    Affected traces are split at the holes, so afterwards the stream holds
    contiguous segments and the caller's ``st.merge(fill_value=...)`` bridges
    them exactly like any other gap. (``merge`` alone would not: it only
    fills between traces, never inside a single masked trace.)
    """
    n_masked = 0
    touched = False
    for tr in st:
        data = tr.data
        if data.dtype.kind not in ("i", "u"):
            continue
        values = np.ma.getdata(data)
        bad = np.abs(values.astype(np.int64)) > max_abs
        if not bad.any():
            continue
        n = int(bad.sum())
        n_masked += n
        first = int(np.argmax(bad))
        if logger is not None:
            logger.warning(
                "%s: %d impossible sample(s) (|x| > %d) masked as gaps, first at %s (value %d)",
                tr.id, n, max_abs, tr.stats.starttime + first / tr.stats.sampling_rate, int(values[first]),
            )
        mask = np.ma.getmaskarray(data) | bad
        tr.data = np.ma.masked_array(values, mask=mask)
        touched = True
    if touched:
        st.traces = st.split().traces
    return n_masked
