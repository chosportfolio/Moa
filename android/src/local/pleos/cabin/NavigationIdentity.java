package local.pleos.cabin;

final class NavigationIdentity {
    private NavigationIdentity() { }

    static boolean validCoordinates(double latitude, double longitude) {
        return Double.isFinite(latitude) && Double.isFinite(longitude)
            && latitude >= -90 && latitude <= 90 && longitude >= -180 && longitude <= 180;
    }

    static boolean nearby(double lat1, double lon1, double lat2, double lon2) {
        if (!validCoordinates(lat1, lon1) || !validCoordinates(lat2, lon2)) return false;
        double lat = Math.toRadians(lat2 - lat1);
        double lon = Math.toRadians(lon2 - lon1);
        double a = Math.sin(lat / 2) * Math.sin(lat / 2)
            + Math.cos(Math.toRadians(lat1)) * Math.cos(Math.toRadians(lat2))
            * Math.sin(lon / 2) * Math.sin(lon / 2);
        return 6371000 * 2 * Math.asin(Math.min(1, Math.sqrt(a))) <= 150;
    }

    static String normalized(String value) {
        return value == null ? "" : value.replaceAll("\\s+", "").toLowerCase(java.util.Locale.ROOT);
    }

    static boolean sameDestination(double expectedLat, double expectedLon,
            String expectedName, String expectedAddress, double latitude, double longitude,
            String name, String address) {
        // Nearby shops can share an entrance or building. Distance alone cannot identify a POI.
        return nearby(expectedLat, expectedLon, latitude, longitude)
            && sameArrival(expectedName, expectedAddress, name, address);
    }

    static boolean sameArrival(String expectedName, String expectedAddress, String name, String address) {
        String expected = normalized(expectedName);
        String arrived = normalized(name);
        String expectedAddr = normalized(expectedAddress);
        String arrivedAddr = normalized(address);
        if (!expected.isEmpty() && !arrived.isEmpty()) {
            return expected.equals(arrived)
                && (expectedAddr.isEmpty() || arrivedAddr.isEmpty() || expectedAddr.equals(arrivedAddr));
        }
        return !expectedAddr.isEmpty() && expectedAddr.equals(arrivedAddr);
    }
}
