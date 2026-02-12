# Scientific Methods

This document describes the data processing methods implemented in the El Paso seismic pipeline, suitable for inclusion in a publication methods section.

---

## 1. Data Acquisition

Continuous seismic waveform data are acquired from 11 stations in the El Paso, Texas metropolitan area and surrounding region. The network comprises 8 Raspberry Shake RS3D three-component geophones, 2 Raspberry Shake RS4D combined geophone/accelerometer instruments, and 1 broadband seismometer (EP.KIDD). All Raspberry Shake stations are registered under the citizen-science AM network.

### Station Network

| Station | Model | Channels | Sample Rate | Latitude | Longitude | Elevation (m) |
|---------|-------|----------|-------------|----------|-----------|----------------|
| AM.R0F2D | RS3D | EHZ/EHN/EHE | 100 Hz | 31.6486 | -106.4603 | 1196.0 |
| AM.R3E95 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.6667 | -106.1828 | 1228.3 |
| AM.R4B41 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.8288 | -106.5315 | 1258.0 |
| AM.R5912 | RS4D | EHZ + ENZ/ENN/ENE | 100 Hz | 32.3333 | -106.7199 | 1303.0 |
| AM.R9070 | RS4D | EHZ + ENZ/ENN/ENE | 100 Hz | 32.2523 | -106.7146 | 1220.0 |
| AM.RADA1 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.9189 | -106.5789 | 1183.0 |
| AM.RC9B8 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.4595 | -106.0899 | 1121.0 |
| AM.RDB14 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.8018 | -106.2277 | 1222.0 |
| AM.RE2E7 | RS3D | EHZ/EHN/EHE | 100 Hz | 31.9279 | -106.4433 | 1274.0 |
| AM.S50BE | RS3D | EHZ/EHN/EHE | 100 Hz | 31.6757 | -106.0986 | 1225.0 |
| EP.KIDD | Broadband | HHZ/HHN/HHE | 100 Hz | 31.7718 | -106.5064 | 1162.5 |

The network spans approximately 1.0 degrees in both latitude (31.46 to 32.33 N) and longitude (-106.72 to -106.09 W), centered near 31.85 N, 106.40 W.

### Data Retrieval

Waveform data and station metadata are retrieved via the FDSN Web Services (FDSNWS) protocol. Raspberry Shake data are fetched from `https://data.raspberryshake.org`; broadband station EP.KIDD data are retrieved from IRIS. Data are requested in 1-hour chunks to manage server load and enable incremental processing. Station metadata (StationXML with full instrument response) are cached locally and refreshed every 7 days.

The pipeline includes a 6-hour data latency offset to account for the typical delay in Raspberry Shake data availability. Previously downloaded chunks are tracked in an SQLite database to prevent redundant requests.

---

## 2. Waveform Preprocessing

Raw waveforms undergo a standard preprocessing sequence to remove instrument effects and isolate the seismic signal band of interest.

### Processing Steps

For each raw miniSEED file, the following operations are applied in order:

1. **Trace merging**: Overlapping or adjacent traces (from chunked 1-hour downloads) are merged using ObsPy's `merge(method=1, fill_value=0)` to produce continuous day-long records.

2. **Detrending**: The mean is removed (demean), followed by linear trend removal.

3. **Tapering**: A 5% Hann (cosine-squared) taper is applied to each end of the trace to suppress spectral leakage during subsequent frequency-domain operations.

4. **Instrument response removal**: The full instrument response (from StationXML) is deconvolved from the data using ObsPy's `remove_response()` method. The operation produces ground velocity (m/s) output. A cosine pre-filter with corners at [0.5, 1.0, 40.0, 45.0] Hz is applied to stabilize the deconvolution at band edges; the upper corners are set well below the Nyquist frequency (50 Hz) to avoid amplifying noise in the poorly characterized frequency range near the digitizer's anti-alias filter rolloff (~40 Hz). A water level of 60 dB is used to prevent spectral division instabilities at frequencies where the instrument response amplitude is small.

No additional bandpass filter is applied after response removal. The cosine pre-filter taper in `remove_response()` provides sufficient band-limiting, and applying a separate bandpass with the same corner frequencies would cause redundant double-filtering, distorting amplitudes near the band edges (Scherbaum, 2001).

The preprocessing converts all station types (geophones, accelerometers, broadband seismometers) to a common ground velocity representation, enabling uniform phase detection across heterogeneous instrumentation.

---

## 3. Phase Detection and Picking

Seismic phase arrivals are identified using PhaseNet (Zhu and Beroza, 2019), a deep-learning phase picker based on a U-Net architecture trained on the Northern California Earthquake Data Center (NCEDC) dataset.

### PhaseNet Implementation

The pipeline uses the PhaseNet implementation provided by SeisBench (Woollam et al., 2022) with the "original" pretrained weights. PhaseNet operates on three-component waveform data using a sliding window of 3001 samples (30.01 s at 100 Hz) with 50% overlap (1500 samples). For single-component stations (RS4D fallback to vertical geophone), PhaseNet processes the available channel.

Pick probability thresholds are set to 0.3 for both P-wave and S-wave arrivals. This relatively permissive threshold allows the subsequent association step to select high-quality picks while preserving weaker arrivals that may contribute to event detection at multiple stations.

### Amplitude Measurement

For each detected pick, the peak absolute velocity amplitude is measured on the vertical (Z) component within an asymmetric window from 0.5 s before to 2.0 s after the pick time. These velocity amplitudes are used downstream for local magnitude computation.

### Channel Selection Strategy

- **RS3D stations**: All three geophone channels (EHZ, EHN, EHE) are provided to PhaseNet for 3-component detection.
- **RS4D stations**: The three accelerometer channels (ENZ, ENN, ENE) are preferred, as they are converted to velocity during preprocessing. If accelerometer data are unavailable, the pipeline falls back to the vertical geophone channel (EHZ) for single-component detection.
- **Broadband stations**: All three seismometer channels (HHZ, HHN, HHE) are used.
- If fewer than 3 components are available for an expected 3-component station, detection proceeds with available data and a warning is logged.

---

## 4. Phase Association and Event Location

Detected P- and S-wave arrivals are grouped into seismic events and preliminary hypocenter locations are estimated using the GaMMA (Gaussian Mixture Model Association) algorithm (Zhu et al., 2022).

### Coordinate Projection

Station coordinates are projected from geographic (latitude, longitude) to a local Cartesian (x, y in km) coordinate system using a stereographic projection centered on the network centroid (31.85 N, 106.40 W). Station depths are computed from elevation (positive downward for GaMMA compatibility).

### Association Parameters

The association uses the Bayesian Gaussian Mixture Model (BGMM) method with the following configuration:

- **Velocity model**: Homogeneous 1D model with P-wave velocity of 6.0 km/s and S-wave velocity of 3.47 km/s (Vp/Vs = 1.73). These values are consistent with published upper-crustal velocities for the El Paso / Rio Grande Rift region (Wilson et al., 2005; Keller and Baldridge, 1999).
- **DBSCAN pre-clustering**: Enabled with epsilon = 25 s and minimum samples = 3, to group temporally proximate picks before Gaussian mixture fitting. The epsilon value is set to approximately the maximum P-wave travel time across the network plus the maximum P-S differential time, with a margin for uncertainty.
- **Search region**: +/- 0.7 degrees in latitude and longitude from the network center, 0-30 km depth. The search bounds extend at least 0.2 degrees beyond the outermost stations to allow location of events just outside the network.
- **Minimum picks per event**: 6 (GaMMA internal parameter), consistent with requiring at least 3 stations with both P and S picks
- **Oversample factor**: 5 (controls the number of initial Gaussian components)
- **Maximum residual standard deviations**: 2.0 s (time) and 2.0 (log10 amplitude)
- **Pick probability filter**: Picks with PhaseNet probability below 0.5 are excluded before association. This two-tier filtering strategy (detection at 0.3, association at 0.5) preserves weak-event sensitivity while reducing false picks entering the association algorithm.
- **Amplitude usage**: Enabled (GaMMA uses amplitude information in the association likelihood)

### Post-Association Quality Control

After GaMMA association, events are filtered to require picks from at least 3 unique stations. Events with picks from fewer stations are discarded.

---

## 5. Magnitude Computation

Local magnitudes (ML) are computed using the Hutton and Boore (1987) attenuation relation, which is the standard for the southern California and western US regional networks.

### Method

For each event, per-station ML values are computed as:

```
ML_station = log10(A_WA) + a * log10(r / r_ref) + b * (r - r_ref) + c
```

where:
- **A_WA** is the simulated Wood-Anderson peak displacement amplitude (mm)
- **r** is the hypocentral distance (km), computed from the Haversine great-circle distance plus event depth
- **a** = 1.110 (geometric spreading coefficient)
- **b** = 0.00189 (anelastic attenuation coefficient)
- **c** = 3.0 (station correction / reference level)
- **r_ref** = 100.0 km (reference distance)

### Velocity-to-Wood-Anderson Conversion

PhaseNet pick amplitudes are measured as peak absolute ground velocity (m/s) on the vertical component. These are converted to equivalent Wood-Anderson displacement using:

```
A_WA (mm) = A_vel (m/s) * WA_gain * 1000 / (2 * pi * f)
```

where:
- **WA_gain** = 2800 (Wood-Anderson static magnification, revised value from Uhrhammer and Collins, 1990)
- **f** = 5.0 Hz (assumed dominant frequency for the velocity-to-displacement conversion; this is an approximation appropriate for local events recorded on 4.5 Hz geophones)
- The factor of 1000 converts from meters to millimeters

### Aggregation

The event magnitude is the arithmetic mean of all per-station ML values. The magnitude uncertainty is reported as the half-range (half the difference between maximum and minimum station ML values). Stations closer than 10.0 km hypocentral distance are excluded from the ML computation, consistent with the calibration range of the Hutton and Boore (1987) attenuation relation.

### Caveats

The Hutton and Boore (1987) attenuation coefficients (a=1.110, b=0.00189, c=3.0) were calibrated for southern California. No published region-specific ML coefficients exist for the El Paso / Rio Grande Rift area. The Rio Grande Rift has higher heat flow and potentially different crustal attenuation properties, which may introduce a systematic magnitude bias of 0.1-0.3 units at hypocentral distances exceeding 50 km. These coefficients are retained for internal catalog consistency and comparability with other western US networks, pending regional calibration with sufficient reference events.

---

## 6. Event Catalog

Daily event files are merged into a running catalog with globally unique event identifiers in the format `ep{YYYYMMDD}-{NNNN}` (e.g., `ep20260115-0003`), where the prefix "ep" denotes El Paso and the four-digit suffix is the daily event index. The catalog is updated incrementally; days already present are not re-processed unless a full rebuild is requested.

---

## References

Hutton, L. K., and Boore, D. M. (1987). The ML scale in southern California. *Bulletin of the Seismological Society of America*, 77(6), 2074-2094.

Keller, G. R., and Baldridge, W. S. (1999). The Rio Grande Rift: A geological and geophysical overview. *GSA Special Paper*, 291.

Scherbaum, F. (2001). *Of Poles and Zeros: Fundamentals of Digital Seismology*. Springer.

Uhrhammer, R. A., and Collins, E. R. (1990). Synthesis of Wood-Anderson seismograms from broadband digital records. *Bulletin of the Seismological Society of America*, 80(3), 702-716.

Wilson, D., Aster, R., West, M., Ni, J., Grand, S., Gao, W., Baldridge, W. S., Semken, S., and Patel, P. (2005). Lithospheric structure of the Rio Grande Rift. *Nature*, 433, 851-855.

Woollam, J., Muenzel, A., Munoz-Ibanez, D., Rettenberger, S., Woith, H., Tilmann, F., and Bindi, D. (2022). SeisBench -- A Toolbox for Machine Learning in Seismology. *Seismological Research Letters*, 93(3), 1695-1709.

Zhu, W., and Beroza, G. C. (2019). PhaseNet: A deep-neural-network-based seismic arrival-time picking method. *Geophysical Journal International*, 216(1), 261-273.

Zhu, W., McBrearty, I. W., Mousavi, S. M., Ellsworth, W. L., and Beroza, G. C. (2022). Earthquake Phase Association using a Bayesian Gaussian Mixture Model. *Journal of Geophysical Research: Solid Earth*, 127(5), e2021JB023249.
