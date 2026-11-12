package local.pleos.cabin;

/** A proven native mapping can change spelling, never ownership guards. */
public final class NativeCanonicalRouteTest {
    private static int passed;
    private static void check(boolean value, String label) {
        if (!value) throw new AssertionError(label);
        passed++;
    }
    public static void main(String[] args) {
        String external = "개천식당 (Gaecheonsikdang)";
        NativePoiCache cache = new NativePoiCache();
        cache.putNative("개천식당", 37, 127, "native-1", "native", "대전 동구 시장로 1", "destination");
        NativePoiCache.Ref poi = cache.get(external, "", 37, 127);
        check(poi != null, "The callback first proves the external/native place correspondence");
        check(!NativeNavigation.routeStartNameMatches(external, null, "개천식당"), "Unverified spelling must not loosen the route guard");
        check(NativeNavigation.routeStartNameMatches(external, poi, "개천식당"), "A verified canonical name can identify the route start");
        check(!NativeNavigation.routeStartNameMatches(external, poi, "개천식당 강남점"), "Another branch cannot identify this route start");
        check(!NativeNavigation.nativeDestinationMatches(null, 37, 127, external,
            37, 127, "개천식당", "대전 동구 시장로 1"), "Destination names remain strict without verified correspondence");
        check(NativeNavigation.nativeDestinationMatches(poi, 37, 127, external,
            37, 127, "개천식당", "대전 동구 시장로 1"), "The verified canonical destination can confirm guidance");
        check(!NativeNavigation.nativeDestinationMatches(poi, 37, 127, external,
            37, 127, "다른 식당", "대전 동구 시장로 1"), "Same coordinates do not identify a different store");
        check(!NativeNavigation.nativeDestinationMatches(poi, 37, 127, external,
            37, 127, "개천식당", "대전 동구 시장로 2"), "Conflicting native addresses still reject the destination");
        check(!NativeNavigation.nativeDestinationMatches(poi, 37, 127, external,
            38, 127, "개천식당", "대전 동구 시장로 1"), "A distant same-name branch cannot confirm guidance");

        NativePoiCache offset = new NativePoiCache();
        offset.putNative("개천식당", 37, 127.0015, "native-2", "native", "", "bookmark");
        NativePoiCache.Ref nearby = offset.get(external, "", 37, 127);
        check(nearby != null, "A nearby public native entrance may identify the original candidate");
        check(!NativeNavigation.nativeDestinationMatches(nearby, 37, 127, external,
            37, 127.003, "개천식당", ""), "Native-coordinate matching cannot expand the original 150-metre boundary");
        System.out.println("Verified native route identity: " + passed + " passed.");
    }
}
