# ADR-002: GaMMA Over PyOcto/HypoDD for Phase Association

## Status

Accepted

## Context

After phase picking, the pipeline must group individual P and S arrivals into seismic events and estimate preliminary hypocenter locations. The main candidates are:

- **GaMMA** (Gaussian Mixture Model Association; Zhu et al., 2022): Bayesian probabilistic association that jointly estimates event locations and pick assignments.
- **PyOcto** (Earthquake Associator): Grid-search-based associator that requires a pre-defined velocity model and search grid.
- **HypoDD** (Waldhauser and Ellsworth, 2000): Double-difference relocation algorithm that requires initial event locations.
- **REAL** (Rapid Earthquake Association and Location): Backprojection-based associator.

## Decision

We chose GaMMA with the Bayesian Gaussian Mixture Model (BGMM) method for phase association and initial event location.

## Consequences

**Positive**:
- GaMMA does not require initial event locations or a detailed velocity model. A simple homogeneous 1D velocity model (Vp=5.5 km/s, Vs=3.18 km/s) is sufficient for association.
- The Bayesian approach naturally handles noise picks and provides uncertainty estimates (sigma_time, sigma_amp) for each event.
- GaMMA incorporates amplitude information in the association likelihood, improving discrimination between real events and noise clusters.
- DBSCAN pre-clustering efficiently reduces the problem size for large numbers of picks.
- GaMMA integrates well with the PhaseNet pick format (timestamp, type, probability, amplitude).
- The Python API allows direct integration without subprocess calls or file-format conversions.

**Negative**:
- GaMMA locations are preliminary and may have systematic biases due to the simplified velocity model.
- The homogeneous 1D velocity model does not account for lateral velocity variations across the 1-degree network aperture.
- GaMMA is actively developed and the API may change between versions (mitigated by pinning the Git commit).

## Alternatives Considered

- **PyOcto**: Requires a well-defined velocity model and search grid, which adds configuration complexity. Less natural integration with probabilistic pick weights. Rejected for the higher setup burden.
- **HypoDD**: A relocation algorithm, not an associator. Requires initial locations (from another tool) and is designed for improving relative locations of clustered events. Not suitable as a primary association method.
- **REAL**: Fast backprojection-based method. Less flexible than GaMMA for incorporating pick probabilities and amplitudes. Rejected in favor of GaMMA's probabilistic framework.
- **Manual association with grid search**: Traditional approach using travel-time grids. Rejected due to higher complexity and difficulty handling the sparse, noisy pick data from citizen-science instruments.
