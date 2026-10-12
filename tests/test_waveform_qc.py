from __future__ import annotations

import logging

import numpy as np
from obspy import Stream, Trace, UTCDateTime

from lib.waveform_qc import MAX_ABS_COUNTS, mask_impossible_samples


def _tr(data, dtype=np.int32):
    tr = Trace(data=np.asarray(data, dtype=dtype))
    tr.stats.network, tr.stats.station, tr.stats.channel = "AM", "R0F2D", "EHN"
    tr.stats.sampling_rate = 100.0
    tr.stats.starttime = UTCDateTime("2026-10-06T02:20:00")
    return tr


def test_clean_integer_trace_is_untouched():
    data = (np.random.default_rng(0).integers(-20000, 20000, 1000)).astype(np.int32)
    st = Stream([_tr(data)])
    assert mask_impossible_samples(st) == 0
    assert not np.ma.is_masked(st[0].data)
    np.testing.assert_array_equal(st[0].data, data)


def test_corrupt_samples_become_gaps_and_are_interpolated():
    data = np.full(1000, 17000, dtype=np.int32)
    data[500:503] = [536888180, -536870912, 536871366]  # the 2026-10-06 shape
    st = Stream([_tr(data)])

    n = mask_impossible_samples(st, logger=logging.getLogger("qc"))

    assert n == 3
    assert len(st) == 2  # split at the hole
    st.merge(method=1, fill_value="interpolate")
    assert len(st) == 1
    out = st[0].data
    assert not np.ma.is_masked(out)
    assert np.abs(out).max() <= MAX_ABS_COUNTS
    assert out[499] == 17000 and out[503] == 17000


def test_full_scale_24bit_sample_is_kept():
    data = np.array([0, 2**23, -(2**23), 0], dtype=np.int32)
    st = Stream([_tr(data)])
    assert mask_impossible_samples(st) == 0


def test_float_traces_are_skipped():
    st = Stream([_tr([0.0, 1e12, 0.0], dtype=np.float32)])
    assert mask_impossible_samples(st) == 0


def test_split_yields_segments_around_the_hole():
    data = np.full(1000, 100, dtype=np.int32)
    data[600] = 2**30
    st = Stream([_tr(data)])
    mask_impossible_samples(st)
    assert len(st) == 2
    assert sum(tr.stats.npts for tr in st) == 999
    assert st[1].stats.starttime == st[0].stats.endtime + 0.02
