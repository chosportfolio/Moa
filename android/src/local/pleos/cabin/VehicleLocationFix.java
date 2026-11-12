package local.pleos.cabin;

/** SDK values, kept in memory and never converted from confidence to metres. */
final class VehicleLocationFix {
    final double latitude;
    final double longitude;
    final Float confidence;
    final long timestampMillis;

    private VehicleLocationFix(double latitude, double longitude, Float confidence, long timestampMillis) {
        this.latitude = latitude;
        this.longitude = longitude;
        this.confidence = confidence;
        this.timestampMillis = timestampMillis;
    }

    static VehicleLocationFix from(double latitude, double longitude, float confidence,
            long timestampMillis, long nowMillis) {
        if (!Double.isFinite(latitude) || !Double.isFinite(longitude)
                || latitude < -90 || latitude > 90 || longitude < -180 || longitude > 180
                || timestampMillis <= 0 || timestampMillis > nowMillis + 5000
                || timestampMillis < nowMillis - 30000) {
            return null;
        }
        return new VehicleLocationFix(latitude, longitude,
            Float.isFinite(confidence) ? confidence : null, timestampMillis);
    }
}
