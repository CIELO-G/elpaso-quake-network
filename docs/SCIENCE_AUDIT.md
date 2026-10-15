# Science Audit: El Paso Seismic Processing Pipeline

**Auditor**: Agent 1 (Scientific Validation)
**Date**: 2026-02-11
**Scope**: All scientific computations in Steps 2-5

---

## Network Geometry Summary

Before discussing parameters, key network facts derived from `stations.json`:

| Metric | Value |
|--------|-------|
| Number of stations | 11 (9 RS3D, 2 RS4D, 1 broadband EP.KIDD) |
| Latitude range | 31.46 to 32.33 (0.87 deg, ~97 km N-S) |
| Longitude range | -106.72 to -106.09 (0.63 deg, ~55 km E-W) |
| Max station separation | R5912-RC9B8: 114 km |
| Min station separation | R4B41-KIDD: 6.8 km |
| Max P-S time | ~15 s (across full network, Vp=5.5) |
| Sample rate | 100 Hz (all stations), Nyquist = 50 Hz |
| Tectonic setting | Rio Grande Rift / Basin and Range |

---

## Issue 1: Pre-filter Corners Too Close to Nyquist

**Severity**: MAJOR
**File**: `2-processing/process.py:42`, `2-processing/config.yaml:28`
**Current**: `pre_filt = [0.5, 1.0, 45.0, 49.0]`

### Problem
The upper pre-filter corners (f3=45 Hz, f4=49 Hz) are dangerously close to the Nyquist frequency (50 Hz). At 49 Hz, the anti-alias filter response of a 100 Hz digitizer is already rolling off significantly. The spectral division in response removal amplifies noise at frequencies where the instrument response is poorly characterized. Standard IRIS/USGS practice is to keep f4 at least 5 Hz below Nyquist.

The Raspberry Shake digitizer uses a simple single-pole anti-alias filter at ~40 Hz (for the 100 sps stream), meaning the response characterization above ~40 Hz is unreliable.

### Recommended Fix
```yaml
pre_filt: [0.5, 1.0, 40.0, 45.0]
```
This keeps the flat passband below the digitizer's reliable range and tapers to zero well before Nyquist.

### References
- Haney et al. (2012), USGS Open-File Report 2012-1034 (ObsPy response removal best practices)
- IRIS DMC recommendations: f4 <= 0.9 * Nyquist for broadband; lower for short-period

### Impact
- Without fix: Spurious high-frequency noise injected into processed traces, especially at stations with poor SNR. This can cause false PhaseNet picks on noise artifacts.
- With fix: Cleaner spectral content above 40 Hz; negligible loss of seismic signal (earthquake energy is predominantly below 30 Hz for local events).

---

## Issue 2: Redundant Double-Filtering

**Severity**: MAJOR
**File**: `2-processing/process.py:170-177`
**Current**: Response removal with `pre_filt` (cosine taper at band edges), THEN separate 4th-order zero-phase Butterworth bandpass [1.0-45.0 Hz].

### Problem
The pre-filter in `remove_response()` already applies a cosine taper that rolls off below 1.0 Hz and above 45.0 Hz. Applying an additional bandpass filter with the same corner frequencies results in **double-filtering** at the band edges, creating an unnecessarily steep roll-off. For zero-phase Butterworth (effective 8th order), this further compounds to an extremely aggressive filter that can:

1. Distort waveforms near corner frequencies, affecting arrival time picks.
2. Introduce ringing artifacts (Gibbs phenomenon) near impulsive arrivals.
3. Reduce amplitude accuracy for signals near the filter corners.

### Recommended Fix
**Remove the separate bandpass filter entirely.** The `pre_filt` cosine taper in `remove_response()` is sufficient for band-limiting. If additional filtering is desired for specific analysis, it should be done downstream (e.g., before amplitude measurement) rather than baked into the processed waveforms.

Alternatively, if a separate bandpass is needed for noise control, use **narrower** corners than the pre-filter:
```python
# Only if needed: slightly narrower than pre_filt to avoid double-filtering
st.filter("bandpass", freqmin=1.5, freqmax=35.0, corners=4, zerophase=True)
```

### References
- Scherbaum (2001), "Of Poles and Zeros", Ch. 4 (cascaded filter effects)
- ObsPy documentation: `remove_response()` pre_filt already performs band-limiting

### Impact
- Without fix: Amplitude measurements near 1 Hz and 45 Hz are systematically attenuated. ML magnitudes may be biased for low-frequency events. PhaseNet may miss emergent arrivals with borderline SNR near filter edges.
- With fix: Flat instrument response across the passband; more reliable amplitudes.

---

## Issue 3: Zero-Phase Filter Acausality and PhaseNet

**Severity**: MINOR
**File**: `2-processing/process.py:176` (`filter_zerophase: True`)

### Problem
Zero-phase (acausal) Butterworth filtering introduces precursory ringing on impulsive arrivals. The reverse-pass of the filter leaks energy **before** the true arrival onset. PhaseNet was trained on real seismic data that is not uniformly zero-phase filtered, so this could slightly impact pick timing.

However, PhaseNet is generally robust to this, and zero-phase filtering preserves arrival timing of the amplitude envelope. This is a minor concern.

### Recommended Fix
Keep zero-phase filtering for the processed waveforms. This is standard practice. The precursory artifacts are small for a 4th-order filter and do not significantly impact PhaseNet picks. No change needed.

### Impact
Negligible. PhaseNet was trained on varied processing flows and handles this well.

---

## Issue 4: Hann Taper Fraction on Full-Day Traces

**Severity**: MINOR
**File**: `2-processing/process.py:156`, config `taper_fraction: 0.05`

### Problem
A 5% taper on a 24-hour trace means 5% x 86400s = 4320 seconds = **72 minutes** of data tapered at each end. This is a significant amount of data, especially for detecting events near the boundaries of the daily processing window.

However, this is applied before response removal, where edge effects from spectral division can be severe. The taper prevents response removal artifacts at trace edges.

### Recommended Fix
The 5% taper is actually appropriate for full-day traces given that response removal is the primary concern. The alternative would be to:
1. Process slightly overlapping windows (e.g., fetch 25 hours, taper, process, then trim to 24 hours).
2. Use a smaller taper (1-2%) with `max_percentage` parameter.

For now, 5% is acceptable as-is. Events near day boundaries may be picked on adjacent days where they are not tapered. **No change required.**

### Impact
Minor: ~72 minutes at each end of the day have attenuated amplitudes. Events in these windows will have slightly reduced detection probability. The daily overlap in the pipeline's scheduled mode partially mitigates this.

---

## Issue 5: Water Level 60 dB for Raspberry Shake

**Severity**: MINOR
**File**: `2-processing/config.yaml:34`, `water_level: 60`

### Problem
A water level of 60 dB is commonly used for broadband instruments. For Raspberry Shake geophones (RS3D/RS4D), which have a relatively simple response and high self-noise below their corner frequency (~4.5 Hz), this is acceptable and possibly even conservative.

### Recommended Fix
60 dB is appropriate. This prevents division-by-zero artifacts in the spectral deconvolution without over-smoothing the response. IRIS typically recommends 60 dB. **No change required.**

### References
- Krischer et al. (2015), ObsPy documentation: default water_level=60

### Impact
None. This is standard practice.

---

## Issue 6: PhaseNet P/S Thresholds Too Low

**Severity**: MAJOR
**File**: `3-detection/config.yaml:26-27`, `p_threshold: 0.3`, `s_threshold: 0.3`

### Problem
While the original PhaseNet paper (Zhu & Beroza, 2019) uses 0.3 as a demonstration threshold, production deployments typically use 0.5 or higher to reduce false picks. With only 11 stations, every false pick consumes a larger fraction of the pick set fed to GaMMA. The association step has `min_pick_probability: 0.4` (filtering at association time), but this still lets 0.3-0.4 picks into the detection CSV, wasting storage and processing.

For a sparse network in a moderate-seismicity region like the Rio Grande Rift, the balance between sensitivity and precision favors a higher threshold.

### Recommended Fix
```yaml
p_threshold: 0.3    # Keep as-is for detection (captures weak events)
s_threshold: 0.3    # Keep as-is for detection
```

**However**, the real filtering should happen at association time. The current `min_pick_probability: 0.4` in the association config is the correct place to tune this. Consider:
- For initial catalog building: keep detection at 0.3, association filter at 0.4 (current)
- For production quality: raise detection to 0.5/0.5 OR raise association filter to 0.5

**Recommended approach**: Keep 0.3/0.3 at detection stage (preserving weak event sensitivity) but raise `min_pick_probability` to **0.5** at association. This is a two-tier filtering strategy used by major networks (e.g., SCSN, NCEDC).

### References
- Zhu & Beroza (2019), GJI: threshold 0.3 used for benchmarking
- Park et al. (2020), SRL: production PhaseNet uses 0.5
- Mousavi et al. (2020), Nature Communications: EQTransformer recommends 0.5 for production

### Impact
- Current (0.3 detection, 0.4 association): Good sensitivity but higher false positive rate, more spurious associations
- Recommended (0.3 detection, 0.5 association): ~20% fewer picks into association, significantly fewer false events, minimal loss of real events

---

## Issue 7: Amplitude Measurement Window Too Narrow for S-waves

**Severity**: MAJOR
**File**: `3-detection/detect.py:44`, config `amp_window_after: 2.0`

### Problem
The amplitude measurement window is [pick - 0.5s, pick + 2.0s] for **all** picks (both P and S). This is reasonable for P-waves, where the maximum amplitude typically arrives within 1-2 seconds of the first motion. However, for S-waves, the peak amplitude of the surface-wave coda can arrive 5-10 seconds after the S onset, especially for local/regional events.

For ML computation (Hutton & Boore, 1987), the amplitude should be the peak amplitude on a Wood-Anderson seismogram, which traditionally means the largest amplitude in the entire S-wave train. Using only 2 seconds after the S pick systematically **underestimates** the peak amplitude and therefore **underestimates ML**.

### Recommended Fix
Use phase-dependent amplitude windows:
```yaml
amp_window_p_before: 0.5
amp_window_p_after: 2.0
amp_window_s_before: 0.5
amp_window_s_after: 5.0
```

In `detect.py`, modify `measure_amplitudes()` to select the window based on the pick phase:
```python
if pick["phase"] == "S":
    after = config["amp_window_s_after"]
else:
    after = config["amp_window_p_after"]
```

### References
- Hutton & Boore (1987), BSSA: ML defined using maximum trace amplitude (typically S or surface wave)
- Bormann (2012), IASPEI New Manual of Seismological Observatory Practice, Ch. 3

### Impact
- Without fix: ML systematically underestimated by 0.1-0.5 magnitude units for events where the S-wave coda peak falls outside the 2-second window. This is a **systematic magnitude bias**.
- With fix: More complete sampling of the S-wave train yields more accurate ML.

---

## Issue 8: Amplitude Measured on Vertical Component Only

**Severity**: MINOR
**File**: `3-detection/detect.py:247`

### Problem
The code preferentially measures amplitude on the vertical (Z) component:
```python
tr = next((t for t in traces if t.stats.channel.endswith("Z")), traces[0])
```

For ML, the traditional practice (and the basis for Hutton & Boore calibration) uses the **horizontal** components, as the Wood-Anderson instrument is a horizontal-component torsion seismometer. The maximum amplitude on the horizontal components is typically larger than on the vertical, so measuring on Z alone systematically underestimates ML.

### Recommended Fix
Measure peak amplitude on both horizontal components (N/E or 1/2) and take the geometric mean or maximum, as per standard ML practice. If only the vertical is available (1C stations), apply a correction factor or flag the measurement.

```python
# Prefer horizontal components for ML amplitude
horiz = [t for t in traces if t.stats.channel.endswith(("N", "E", "1", "2"))]
if horiz:
    # Use the maximum amplitude across horizontal components
    ...
else:
    tr = next((t for t in traces if t.stats.channel.endswith("Z")), traces[0])
```

### References
- Hutton & Boore (1987): calibrated on horizontal WA
- Uhrhammer & Collins (1990): ML on horizontals

### Impact
- Without fix: ML underestimated by ~0.1-0.3 units depending on S-wave polarization and incidence angle.
- With fix: More consistent with Hutton & Boore calibration.

---

## Issue 9: RS4D Accelerometer Data with PhaseNet

**Severity**: MINOR
**File**: `3-detection/detect.py:118-119`

### Problem
For RS4D stations, the code uses EN? (accelerometer) channels as the primary detection input, with EHZ (geophone) as fallback. PhaseNet was trained on **velocity** data. The response removal step converts raw counts to velocity (output="VEL"), which means:

- For geophones (EH?, HH?): counts -> velocity. Correct.
- For accelerometers (EN?): counts -> velocity. This means ObsPy integrates the acceleration response to produce velocity output.

This is actually **correct** behavior because `remove_response(output="VEL")` handles the unit conversion automatically via the full instrument response in StationXML. The output is velocity (m/s) regardless of whether the input was a velocity or acceleration sensor.

### Recommended Fix
**No code change needed.** The processing chain is correct. However, verify that the StationXML metadata for RS4D EN? channels correctly describes the accelerometer response (including the integration to velocity). If the StationXML is wrong, the velocity output will be wrong.

**Action item**: Validate EN? StationXML metadata for AM.R5912 and AM.R9070 against Raspberry Shake specifications.

### Impact
If StationXML is correct: None. If incorrect: Could produce completely wrong velocity traces for 2 stations, affecting picks and magnitudes.

---

## Issue 10: 1D Velocity Model Too Slow for Rio Grande Rift

**Severity**: CRITICAL
**File**: `4-association/config.yaml:37-39`, `vel: {P: 5.5, S: 3.18}`

### Problem
The current Vp=5.5 km/s is too low for the upper crust of the El Paso / Rio Grande Rift region. Published crustal models for this area indicate:

- **Wilson et al. (2005)**, USGS: West Texas / southern Rio Grande Rift upper crust Vp = 5.9-6.2 km/s (0-15 km depth)
- **Averill et al. (2007)**: Basin and Range average Vp = 6.0 km/s for upper 15 km
- **Keller & Baldridge (1999)**: Rio Grande Rift Vp = 5.8-6.0 km/s upper crust

The current Vs=3.18 km/s gives Vp/Vs = 1.73, which is reasonable (Poisson solid), but both are too slow.

A 1D velocity model that is too slow will cause GaMMA to:
1. **Overestimate travel times**, leading to overly permissive association (picks that should not belong together get grouped).
2. **Mislocate events**: systematic shift in epicenter and depth toward the center of the network.
3. **Increase location residuals**, causing valid events to be rejected by sigma filters.

### Recommended Fix
```yaml
vel:
  P: 6.0
  S: 3.47
```

This uses Vp=6.0 km/s (El Paso upper crust average) with Vp/Vs=1.73 (standard crustal ratio), giving Vs=3.47 km/s.

For even better results, implement a layered 1D model. GaMMA supports this:
```yaml
vel:
  P: [5.5, 6.0, 6.5]
  S: [3.18, 3.47, 3.76]
  z: [0, 5, 15]
```

### References
- Wilson et al. (2005), USGS Prof. Paper 1707
- Keller & Baldridge (1999), GSA Special Paper 291
- Averill et al. (2007), JGR: Basin and Range crustal structure

### Impact
- Without fix: Systematic mislocation of events (1-5 km bias), false associations from overly permissive travel times, some valid events rejected due to high residuals.
- With fix: More accurate locations (sub-km improvement), fewer false events, more real events retained.

---

## Issue 11: DBSCAN eps=50s Far Too Large

**Severity**: CRITICAL
**File**: `4-association/config.yaml:50`, `dbscan_eps: 50`

### Problem
DBSCAN eps=50 seconds means any two picks within 50 seconds of each other can be clustered together. For this network:

- Maximum P travel time across network: ~21 s (at Vp=5.5, 114 km)
- Maximum P-S time at any single station: ~15 s (for events at network edge)
- Typical P-S time for local events: 2-8 s

An eps of 50 seconds is **3x the maximum travel time across the entire network**. This means picks from **completely separate events** up to 50 seconds apart will be pre-clustered into the same DBSCAN group, forcing GaMMA to try to fit them together. This dramatically increases false association rates and computational cost.

### Recommended Fix
```yaml
dbscan_eps: 25
```

The eps should be approximately: max_travel_time + max_PS_time + margin. For this network: 21s + 15s = 36s. Adding ~20% margin gives ~25-30s. Use 25s to be conservative.

### References
- Zhu et al. (2022), GaMMA paper: recommends eps based on network aperture and expected travel times
- SCSN production GaMMA: eps = 2x max travel time across network

### Impact
- Without fix: Multiple events within 50s get merged into single composite events, or noise picks from different events get mixed, causing location scatter and magnitude errors. Severe impact on catalog quality.
- With fix: Clean event separation; significant reduction in false events.

---

## Issue 12: Search Bounds Too Tight for Network Extent

**Severity**: MAJOR
**File**: `4-association/config.yaml:27-28`, `xlim_degree: 0.5`, `ylim_degree: 0.5`

### Problem
The search region is center +/- 0.5 degrees in both lat and lon:
- Latitude: 31.35 to 32.35
- Longitude: -106.90 to -105.90

But the network spans:
- Latitude: 31.46 to 32.33 (0.87 deg)
- Longitude: -106.72 to -106.09 (0.63 deg)

The **latitude** search window is adequate (0.5 deg half-width vs 0.44 deg needed), but barely. Events occurring just outside the network boundary (which are commonly detectable by the outer stations) may fall outside the search bounds. More critically, the R5912 station at latitude 32.33 is only 0.02 degrees from the northern search boundary (32.35), meaning events just north of this station cannot be located.

### Recommended Fix
```yaml
xlim_degree: 0.7    # ~0.63 deg network E-W extent + 0.2 deg margin each side
ylim_degree: 0.7    # ~0.87 deg network N-S extent + 0.2 deg margin each side
```

Also consider adjusting the center to better reflect the actual network centroid:
```yaml
center_lat: 31.84
center_lon: -106.41
```
(The current values of 31.85 / -106.40 are close enough.)

### References
- Standard practice: search bounds should extend at least 0.2 degrees beyond the outermost stations

### Impact
- Without fix: Events near the network edges (especially near R5912 in the north) may be excluded from the search, causing missed detections.
- With fix: Complete coverage of events detectable by the network, plus reasonable extrapolation beyond.

---

## Issue 13: min_picks_per_eq = 3 Contradicts min_stations_per_eq = 3

**Severity**: MINOR
**File**: `4-association/config.yaml:56-57`

### Problem
`min_picks_per_eq: 3` is passed to GaMMA, while `min_stations_per_eq: 3` is a post-association filter. Since each station can contribute both P and S picks, `min_picks_per_eq=3` could mean 2 picks from station A (P+S) and 1 pick from station B = 3 picks but only 2 stations. This event would then be rejected by the station count filter, wasting association effort.

For an 11-station network, requiring 3 stations (and typically 6+ picks) is appropriate for production quality.

### Recommended Fix
```yaml
min_picks_per_eq: 6     # At least 6 phase picks (3P + 3S minimum)
min_stations_per_eq: 3   # At least 3 unique stations (keep as-is)
```

Raising `min_picks_per_eq` to 6 ensures GaMMA doesn't waste time on events with too few picks. Note the current `min_picks_per_eq=3` in the config contradicts the DEFAULTS which also shows 6 in the code. **Verify which value is actually being used** -- the config.yaml value (3) overrides the code default (6).

Actually, looking at the code defaults more carefully at `associate.py:48`: `"min_picks_per_eq": 6`. But the config.yaml says `min_picks_per_eq: 3`. The config file wins, so **3 is being used**. This should be 6.

### Impact
- Without fix: GaMMA attempts association on events with only 3 picks, producing poorly constrained locations that are later rejected anyway.
- With fix: Faster association, fewer poorly constrained intermediate events.

---

## Issue 14: Hutton & Boore 1987 Coefficients for SoCal, Not Rio Grande Rift

**Severity**: MAJOR
**File**: `4-association/config.yaml:80-85`, `associate.py:336-400`

### Problem
The Hutton & Boore (1987) attenuation coefficients (a=1.110, b=0.00189, c=3.0) were calibrated for **Southern California**, which has different crustal attenuation properties than the Basin and Range / Rio Grande Rift.

The Rio Grande Rift has:
- Higher heat flow (70-100+ mW/m2 vs 60-70 for SoCal)
- Lower Q values (higher attenuation), particularly Qp and Qs
- Different geometric spreading characteristics due to the extensional tectonic regime

Using SoCal coefficients in the Rio Grande Rift likely causes a **systematic magnitude bias**, probably overestimating ML at distances >50 km and underestimating at close range.

### Recommended Fix
There are no published region-specific ML coefficients for the El Paso area. Options:

1. **Use SoCal coefficients as-is** (current approach) -- acceptable for initial catalog, but document the expected bias.
2. **Use Zhu et al. (2018) updated coefficients** for the western US: a=1.117, b=0.00189, c=2.0, r_ref=100. The lower station correction (c=2.0 vs 3.0) may be more appropriate.
3. **Long-term**: Calibrate region-specific coefficients using the accumulating catalog plus reference events from NEIC/ANSS.

For now, keep SoCal coefficients but **document the expected bias** in the catalog metadata. This is standard practice for new networks that lack region-specific calibration.

### References
- Hutton & Boore (1987), BSSA 77(6): SoCal calibration
- Zhu et al. (2018), SRL: Updated western US coefficients
- Earle et al. (2019), USGS: Regional ML variations

### Impact
- Magnitude bias of 0.1-0.3 units at distances >50 km is likely. This is **acceptable** for an initial catalog but should be documented and corrected when sufficient data accumulate for regional calibration.

---

## Issue 15: Wood-Anderson Gain 2080 vs 2800

**Severity**: MAJOR
**File**: `4-association/config.yaml:77`, `ml_wa_gain: 2080`

### Problem
The classic Wood-Anderson static magnification of 2080 was revised by Uhrhammer & Collins (1990) to **2800** based on new calibration measurements. The correction is now widely adopted by USGS, SCSN, and most modern networks. Using the old value of 2080 introduces a systematic ML bias:

```
ML_bias = log10(2800/2080) = +0.129 magnitude units
```

That is, magnitudes computed with gain=2080 are **0.13 units too high** compared to the modern standard.

### Recommended Fix
```yaml
ml_wa_gain: 2800
```

### References
- Uhrhammer & Collins (1990), BSSA 80(3): revised WA magnification
- Bormann (2012), IASPEI Manual: recommends 2800 as standard
- USGS practice: uses 2800 since ~2000

### Impact
- Without fix: All ML values are systematically 0.13 units too high, creating inconsistency with USGS/ANSS reference catalogs.
- With fix: ML values consistent with modern standard. This is a simple constant correction.

---

## Issue 16: Single-Frequency Velocity-to-Displacement Conversion

**Severity**: MAJOR
**File**: `associate.py:353`, `ml_freq_hz: 5.0`

### Problem
The velocity-to-Wood-Anderson conversion assumes a single dominant frequency of 5 Hz:
```python
vel_to_wa_mm = wa_gain * 1000.0 / (2.0 * math.pi * freq)
```

This converts velocity (m/s) to displacement (m) using d = v/(2*pi*f), then multiplies by WA gain and converts to mm. The problem is that 5 Hz is a **hardcoded assumption** that may not match the actual dominant frequency of each event's maximum-amplitude phase.

The sensitivity to this assumption is significant:
- At 2 Hz dominant: ML would be +0.40 higher
- At 3 Hz dominant: ML would be +0.22 higher
- At 5 Hz dominant: baseline (current)
- At 8 Hz dominant: ML would be -0.20 lower
- At 10 Hz dominant: ML would be -0.30 lower

Local events at close range tend to have higher dominant frequencies (8-15 Hz), while events at the network edge may have lower dominant frequencies (2-5 Hz). This creates a **distance-dependent magnitude bias**.

### Recommended Fix
Measure the dominant frequency of each pick's amplitude window by finding the spectral peak or using zero-crossing analysis:
```python
# For each pick's amplitude window:
from scipy.signal import welch
freqs, psd = welch(windowed.data, fs=tr.stats.sampling_rate, nperseg=min(256, len(windowed.data)))
dominant_freq = freqs[np.argmax(psd)]
# Then use this per-pick frequency in the conversion
```

If this is too complex for the initial implementation, a frequency of 5 Hz is a reasonable compromise for local events recorded on short-period instruments (4.5 Hz geophones), but it should be documented as an approximation.

### References
- Havskov & Ottem"ller (2010), "Routine Data Processing in Earthquake Seismology", Ch. 7
- Bormann (2012), IASPEI Manual: discusses frequency-dependent WA simulation

### Impact
- Without fix: Distance-dependent ML bias of +/- 0.2-0.4 units. Events at the network edge (lower freq) are overestimated; close events (higher freq) are underestimated.
- With fix: Per-event frequency measurement removes the systematic bias.

---

## Issue 17: Minimum Distance 1 km for ML (Should Be 10 km)

**Severity**: MAJOR
**File**: `4-association/config.yaml:78`, `ml_min_distance: 1.0`

### Problem
Hutton & Boore (1987) calibrated their attenuation formula for distances r > 10 km. At distances below 10 km, the geometric spreading term `a * log10(r/100)` and the anelastic attenuation term `b * (r - 100)` are extrapolations beyond the calibration range. The formula becomes unreliable at very short distances because:

1. Near-field geometric spreading differs from far-field 1/r behavior.
2. Amplitude saturation effects at close range.
3. Site effects dominate over path effects.

With `ml_min_distance: 1.0`, stations very close to the epicenter contribute ML values that may be wildly inaccurate.

### Recommended Fix
```yaml
ml_min_distance: 10.0
```

This matches the Hutton & Boore calibration range. For an 11-station network with a maximum aperture of 114 km, there should be sufficient stations at 10+ km distance for most events within the network.

### References
- Hutton & Boore (1987), BSSA: calibration range 10-700 km
- Bormann (2012), IASPEI Manual: recommends r > 10 km for ML

### Impact
- Without fix: Stations within 10 km of the epicenter contribute unreliable ML readings, increasing scatter and potentially biasing the mean ML.
- With fix: More accurate ML with reduced scatter. For events directly beneath a station, fewer station readings are available, but those readings are more reliable.

---

## Issue 18: ML Uncertainty as (max-min)/2 Instead of Standard Deviation

**Severity**: MINOR
**File**: `associate.py:397`

### Problem
```python
ml_err = (max(station_mls) - min(station_mls)) / 2.0
```

This measures ML uncertainty as the half-range (max - min)/2, which is:
1. Highly sensitive to outliers (a single bad station reading dominates)
2. Not a standard statistical measure (not comparable with other catalogs)
3. Does not scale properly with the number of readings

Standard practice uses:
- **Standard deviation** of station ML values (most common)
- **Median absolute deviation (MAD)** for robustness against outliers

### Recommended Fix
```python
import numpy as np
ml = np.median(station_mls)  # Use median instead of mean for robustness
ml_err = np.std(station_mls)  # Standard deviation
# OR for more robustness:
# ml_err = np.median(np.abs(np.array(station_mls) - ml)) * 1.4826  # MAD
```

Also consider using the **median** instead of the **mean** for the ML value itself, as it is more robust to outlier station readings.

### References
- Bormann (2012), IASPEI Manual: standard deviation as ML uncertainty
- USGS practice: standard deviation of station magnitudes

### Impact
- Without fix: ML uncertainties are inflated by outlier stations and are not comparable with standard catalogs.
- With fix: Standard uncertainty metric; more meaningful QC thresholds.

---

## Issue 19: Unit Consistency in Signal Chain

**Severity**: MINOR (verified correct)
**Files**: Full pipeline

### Verification
Tracing the full signal path:

1. **Raw miniSEED** (Step 1): integer counts from digitizer
2. **Response removal** (Step 2): `output="VEL"` -> velocity in m/s. Confirmed in `process.py:165`.
3. **PhaseNet detection** (Step 3): Expects velocity input. PhaseNet internally normalizes traces, so the absolute units don't matter for picking, but the relative amplitudes across components must be correct. This is fine since all channels go through the same response removal.
4. **Amplitude measurement** (Step 3): Peak |velocity| in m/s. Stored as `amplitude` in CSV. Confirmed in `detect.py:261`.
5. **ML computation** (Step 4): Reads `amplitude` (m/s), converts to WA displacement in mm. Confirmed in `associate.py:353-379`.

The unit chain is **consistent**. No unit conversion bugs found.

### One concern
The amplitude column in the picks CSV is labeled generically as `amplitude` without specifying units. The ML computation assumes it is velocity in m/s. If any future processing changes the response output from "VEL" to "DISP" or "ACC", the ML computation will silently produce wrong results.

**Recommendation**: Add an `amplitude_units` column to the picks CSV (value: "m/s") for self-documenting output. Low priority.

---

## Issue 20: Merge with fill_value=0 Can Introduce Artifacts

**Severity**: MINOR
**File**: `2-processing/process.py:149`

### Problem
```python
st.merge(method=1, fill_value=0)
```

Using `fill_value=0` to fill gaps in the data creates step discontinuities at gap boundaries. These can:
1. Cause ringing artifacts after filtering
2. Be misidentified as phase arrivals by PhaseNet

### Recommended Fix
Use `fill_value="interpolate"` or `fill_value="latest"` for short gaps, or split into separate traces for long gaps:
```python
st.merge(method=1, fill_value="interpolate")
```

For long gaps (>1 second), it may be better to keep traces separate and let PhaseNet handle them independently.

### Impact
Minor: Only affects traces with data gaps. For continuous Raspberry Shake data, gaps are infrequent but do occur (network outages, buffer overflows).

---

## Summary Table

| # | Issue | Severity | Impact on Catalog |
|---|-------|----------|-------------------|
| 1 | Pre-filter corners too close to Nyquist | MAJOR | False picks from HF noise |
| 2 | Redundant double-filtering | MAJOR | Amplitude bias at band edges |
| 3 | Zero-phase filter acausality | MINOR | Negligible |
| 4 | 72-min taper on day boundaries | MINOR | Missed events at day edges |
| 5 | Water level 60 dB | MINOR | None (appropriate) |
| 6 | PhaseNet thresholds 0.3 | MAJOR | Excess false picks -> false events |
| 7 | S-wave amplitude window too narrow | MAJOR | Systematic ML underestimate |
| 8 | Amplitude on vertical only | MINOR | ML underestimate ~0.1-0.3 |
| 9 | RS4D accelerometer with PhaseNet | MINOR | Correct if StationXML valid |
| 10 | Velocity model Vp=5.5 too slow | CRITICAL | Mislocation, false associations |
| 11 | DBSCAN eps=50s too large | CRITICAL | Merged events, false associations |
| 12 | Search bounds too tight | MAJOR | Missed events at network edges |
| 13 | min_picks_per_eq=3 too low | MINOR | Poorly constrained events |
| 14 | SoCal ML coefficients | MAJOR | 0.1-0.3 magnitude bias |
| 15 | WA gain 2080 (should be 2800) | MAJOR | +0.13 systematic ML bias |
| 16 | Single-frequency WA conversion | MAJOR | +/- 0.2-0.4 distance-dependent bias |
| 17 | Minimum distance 1 km | MAJOR | Unreliable close-range ML |
| 18 | ML uncertainty as half-range | MINOR | Non-standard, outlier-sensitive |
| 19 | Unit consistency | OK | Verified correct |
| 20 | Merge fill_value=0 | MINOR | Artifacts at data gaps |

### Priority Order for Fixes

**Critical (fix immediately)**:
1. Issue 10: Velocity model (Vp=5.5 -> 6.0)
2. Issue 11: DBSCAN eps (50 -> 25)

**Major (fix before production)**:
3. Issue 1: Pre-filter corners (49 -> 45 Hz)
4. Issue 2: Remove redundant bandpass
5. Issue 7: S-wave amplitude window (2s -> 5s)
6. Issue 12: Search bounds (0.5 -> 0.7 degrees)
7. Issue 15: WA gain (2080 -> 2800)
8. Issue 17: Min distance (1 -> 10 km)
9. Issue 16: Per-event frequency measurement (or document 5 Hz assumption)
10. Issue 6: Raise association pick probability filter (0.4 -> 0.5)
11. Issue 14: Document SoCal coefficient bias
12. Issue 13: Raise min_picks_per_eq (3 -> 6)

**Minor (fix when convenient)**:
13. Issue 18: Use std dev for ML uncertainty
14. Issue 8: Horizontal component amplitudes
15. Issue 20: Merge fill_value interpolation
16. Issue 19: Add amplitude_units column

---

## Addendum (2026-07-04): Status of Issues 10 & 11 — deliberate operator retuning

Both critical association issues were fixed as recommended in commit d04f58b
(2026-02-11) and later **deliberately retuned to different values** based on
operational experience with the real event population (overwhelmingly shallow
quarry blasts, not deep tectonic seismicity):

- **Issue 10 (velocity)**: GaMMA `vel` set to P=5.0/S=2.89 km/s (commit
  9243c09, 2026-03-19). Rationale: shallow-source ray paths sample slow Hueco
  Bolson / Rio Grande Rift basin fill, so a lower path-average velocity fits
  observed moveouts better than the basement-appropriate 6.0 km/s. The
  homogeneous-GaMMA vs 6-layer-NLLoc physics mismatch is accepted because all
  published events are manually relocated with NLLoc.
- **Issue 11 (DBSCAN eps)**: set to 60 s (commit 1d69e7d, 2026-02-25).
  Rationale: operator experience shows the larger eps yields more accurate and
  complete associations on this sparse network. The merged-/duplicate-event
  risk is mitigated at review time (dashboard duplicate warning on save,
  `scripts/dedup_catalog.py` sweep).

These are conscious trade-offs, not regressions. If the network ever targets
deeper tectonic events or unreviewed automatic publication, revisit both.
