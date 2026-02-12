# ADR-001: PhaseNet Over EQTransformer for Phase Detection

## Status

Accepted

## Context

The pipeline requires an automated seismic phase picker to identify P-wave and S-wave arrivals in continuous waveform data from 11 stations. The two leading deep-learning phase pickers available through SeisBench are PhaseNet (Zhu and Beroza, 2019) and EQTransformer (Mousavi et al., 2020).

Key considerations for this local network:
- **Speed**: The pipeline processes months of data in backfill mode and must handle daily throughput in continuous mode. Detection is the most computationally expensive step.
- **Accuracy on local networks**: The picker must perform well on local and near-regional events recorded by short-period instruments (Raspberry Shake geophones and accelerometers) with relatively high noise levels compared to professional broadband networks.
- **Three-component support**: Most stations have 3 components; the picker should leverage all available channels.
- **Integration**: The picker must be available through SeisBench for standardized model loading and inference.

## Decision

We chose PhaseNet with the "original" pretrained weights for phase detection.

## Consequences

**Positive**:
- PhaseNet's U-Net architecture is significantly faster than EQTransformer's transformer-based architecture, enabling higher throughput in both backfill and continuous modes.
- PhaseNet produces well-calibrated pick probabilities suitable for downstream association filtering.
- The SeisBench `model.classify()` API provides a clean interface with configurable sliding window overlap and batch size.
- PhaseNet performs well on short-period instruments and local events, as demonstrated in numerous community seismology studies.
- Lower GPU memory requirements allow operation on modest hardware or CPU-only systems.

**Negative**:
- EQTransformer produces detection probabilities as well as pick times, which could be useful for event-level confidence scoring (not currently used).
- EQTransformer may perform slightly better on very low-SNR arrivals due to its attention mechanism.
- PhaseNet's original weights are trained on Northern California data; performance on other regions may vary (mitigated by the permissive 0.3 probability threshold and downstream association filtering).

## Alternatives Considered

- **EQTransformer**: More complex architecture with built-in event detection. Rejected due to slower inference speed (~3-5x slower than PhaseNet) and higher memory requirements, which would be prohibitive for backfilling months of data.
- **GPD (Generalized Phase Detection)**: Older CNN-based picker. Rejected as PhaseNet supersedes it in accuracy on modern benchmarks.
- **Manual STA/LTA picking**: Traditional approach. Rejected due to lower accuracy, higher false positive rate, and inability to leverage three-component data effectively.
