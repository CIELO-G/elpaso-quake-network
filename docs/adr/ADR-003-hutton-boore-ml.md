# ADR-003: Hutton and Boore ML Over Other Magnitude Scales

## Status

Accepted

## Context

The pipeline needs to assign magnitude values to detected seismic events. Options include:

- **ML (Local Magnitude)**: The original Richter scale, adapted with modern attenuation relations. Several regional formulations exist.
- **Mw (Moment Magnitude)**: Physics-based, requires spectral fitting or moment tensor inversion. Most accurate for M > 3 but complex to implement for small events.
- **Md (Duration Magnitude)**: Based on coda duration. Simple but less precise.
- **ML Hutton and Boore (1987)**: The standard ML formulation for southern California / western US networks.

## Decision

We compute local magnitude (ML) using the Hutton and Boore (1987) attenuation relation with a Wood-Anderson static magnification of 2800 (revised value from Uhrhammer and Collins, 1990).

## Consequences

**Positive**:
- Hutton and Boore (1987) is the standard ML formulation used by the SCSN (Southern California Seismic Network) and is well-calibrated for the western US, including the El Paso region.
- ML is straightforward to compute from peak amplitude and hypocentral distance, requiring no spectral analysis.
- The Wood-Anderson simulation uses the Uhrhammer and Collins (1990) revised gain of 2800, which is the modern standard adopted by USGS, SCSN, and all major networks.
- ML values are directly comparable to magnitudes reported by regional networks (SCSN, USBR, TexNet).
- The computation uses velocity amplitudes already measured during the detection step, requiring no additional waveform processing.

**Negative**:
- ML saturates for large events (M > ~6), which is unlikely for this network but is a theoretical limitation.
- The velocity-to-Wood-Anderson conversion assumes a single dominant frequency (5 Hz), which is an approximation.
- The Hutton and Boore coefficients are calibrated for southern California; the El Paso region may have different attenuation characteristics. Station corrections are not currently applied.
- ML is computed from the vertical component only, whereas the original Wood-Anderson instrument recorded horizontal components. This introduces a systematic difference.

## Alternatives Considered

- **Moment magnitude (Mw)**: Requires spectral fitting or full waveform inversion. Too complex for the current pipeline scope and the small magnitudes expected from this network. Could be added as a future enhancement.
- **Duration magnitude (Md)**: Simpler but less physical and harder to calibrate. Requires measuring coda duration, which is unreliable for small events on noisy citizen-science instruments.
- **Other ML formulations** (e.g., Bakun and Joyner, 1984): The Hutton and Boore formulation is more widely used in the western US and better calibrated for the distance range of this network.
